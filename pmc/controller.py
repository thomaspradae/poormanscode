from __future__ import annotations

import json
import os
import pwd
import socket
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .config import PMCConfig, load_repo_config_at
from .context import build_context_bundle
from .db import Database
from .domain import AttemptStatus, ExecutionRequest, ExecutionResult, JobState
from .executors import build_executor
from .gitops import WorktreeManager
from .prompts import builder_prompt
from .reviewer import ReviewerService
from .reporting import Reporter
from .scheduler import Scheduler
from .sandbox import resource_snapshot
from .verifier import verify
from .versioning import (JOB_CONTRACT_VERSION, PROMPT_PROFILE_VERSION, SCHEMA_VERSION,
                         VERIFIER_VERSION, pmc_git_sha, stable_hash)


class LeaseBusy(RuntimeError):
    pass


class _LeaseHeartbeat:
    def __init__(self, db: Database, job_id: str, owner: str, ttl: int):
        self.db, self.job_id, self.owner, self.ttl = db, job_id, owner, ttl
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self):
        if not self.db.acquire_lease(self.job_id, self.owner, self.ttl):
            raise LeaseBusy(f"{self.job_id} is leased by another controller")
        interval = max(5.0, self.ttl / 3)
        def beat():
            while not self.stop.wait(interval):
                if not self.db.renew_lease(self.job_id, self.owner, self.ttl):
                    return
        self.thread = threading.Thread(target=beat, daemon=True, name=f"pmc-lease-{self.job_id}")
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.db.release_lease(self.job_id, self.owner)


class Controller:
    def __init__(self, cfg: PMCConfig):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.worktrees = WorktreeManager(cfg.worktrees_dir)
        self.scheduler = Scheduler(self.db, cfg.exploration_rate, cfg.min_samples_per_candidate)
        self.reporter = Reporter(cfg.runs_dir)
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.db.recover_expired_leases(cfg.max_attempts)

    def _write_event_audit(self, job_id: str) -> None:
        self.reporter.json(job_id, "events.json", self.db.job_events(job_id))

    def ensure_worktree(self, job):
        if job.worktree and job.worktree.exists() and job.baseline_commit:
            return job
        wt, baseline = self.worktrees.create(job.repo, job.id, job.base_branch)
        if self.cfg.verifier_sandbox == "restricted-user":
            if not shutil.which("setfacl"):
                raise RuntimeError("restricted-user verifier requires setfacl")
            controller_user = pwd.getpwuid(os.getuid()).pw_name
            subprocess.run(["setfacl", "-Rm",
                            f"u:{controller_user}:rwX,u:pmc-worker:rwX", str(wt)], check=True)
            subprocess.run(["setfacl", "-Rm",
                            f"d:u:{controller_user}:rwX,d:u:pmc-worker:rwX", str(wt)], check=True)
        job.worktree = wt
        job.baseline_commit = baseline
        self.db.update_job(job)
        self.reporter.record_job(job)
        return job

    def _verification_summary(self, v) -> str:
        return json.dumps(
            {
                "ok": v.ok,
                "changed_files": v.changed_files,
                "patch_lines": v.patch_lines,
                "findings": v.findings,
                "commands": [
                    {"name": c.name, "exit_code": c.exit_code, "seconds": round(c.duration_seconds, 2)}
                    for c in v.commands
                ],
            },
            indent=2,
        )

    def run_job(self, job_id: str, forced_candidate: str | None = None) -> JobState:
        with _LeaseHeartbeat(self.db, job_id, self.owner, self.cfg.lease_ttl_seconds):
            return self._run_job_locked(job_id, forced_candidate)

    def _run_job_locked(self, job_id: str, forced_candidate: str | None = None) -> JobState:
        job = self.ensure_worktree(self.db.get_job(job_id))
        if job.state in {JobState.ACCEPTED, JobState.CANCELLED}:
            raise RuntimeError(f"{job.id} is {job.state}")
        repo_cfg = load_repo_config_at(job.repo, job.baseline_commit)
        prior_feedback = self.db.feedback_text(job.id)
        total_attempts = self.db.attempt_count(job.id)
        failures: list[str] = []
        used: list[str] = []

        while total_attempts < self.cfg.max_attempts:
            attempt_no = total_attempts + 1
            exclude = set()
            if used and len(used) >= self.cfg.same_candidate_retries:
                exclude.add(used[-1])
            if forced_candidate:
                matches = [c for c in self.cfg.candidates if c.name == forced_candidate and c.enabled]
                if not matches:
                    raise RuntimeError(f"forced candidate is unavailable: {forced_candidate}")
                from .domain import SchedulerDecision
                candidate0 = matches[0]
                decision = SchedulerDecision(candidate0, "forced", 0.0, "explicit CLI override")
            else:
                try:
                    decision = self.scheduler.choose(
                        job, self.cfg.candidates, role="builder", exclude=exclude, attempt_no=attempt_no
                    )
                except RuntimeError:
                    # If excluding the last worker emptied the pool, allow it again.
                    decision = self.scheduler.choose(
                        job, self.cfg.candidates, role="builder", attempt_no=attempt_no
                    )
            self.db.record_decision(job.id, attempt_no, decision)
            candidate = decision.candidate
            self.db.register_candidate(candidate)
            used.append(candidate.name)
            reservation_id = self.db.reserve_quota(
                candidate.name, job.id, int(candidate.extra.get("max_tokens", 4096))
            )
            prompt_feedback = "\n\n".join(x for x in [prior_feedback, *failures] if x)
            context_bundle = build_context_bundle(job.worktree, job.request,
                                                  baseline=job.baseline_commit)
            prompt = builder_prompt(job, job.worktree, repo_cfg, prompt_feedback,
                                    context=context_bundle.content)
            req = ExecutionRequest(job, candidate, job.worktree, prompt, attempt_no, prompt_feedback or None)
            self.reporter.text(job.id, f"attempt-{attempt_no:02d}-prompt.txt", prompt + "\n")
            self.db.set_state(job.id, JobState.RUNNING)
            version_snapshot = {
                "pmc_git_sha": pmc_git_sha(), "schema_version": SCHEMA_VERSION,
                "config_hash": stable_hash(asdict(self.cfg)),
                "job_contract_version": JOB_CONTRACT_VERSION,
                "prompt_profile_version": PROMPT_PROFILE_VERSION,
                "context_builder_version": context_bundle.manifest["version"],
                "verifier_version": VERIFIER_VERSION,
                "candidate_id": candidate.name, "candidate_version": candidate.version,
                "candidate_hash": stable_hash(candidate),
                "executor": candidate.executor, "executor_version": "1",
                "provider": candidate.provider or candidate.quota_group,
                "provider_model_id": candidate.model,
                "prompt_profile": candidate.prompt_profile, "tool_profile": candidate.tool_profile,
                "resource_class": candidate.resource_class,
                "base_repository_sha": job.baseline_commit,
                "verification_policy_hash": stable_hash(repo_cfg),
            }
            self.reporter.json(job.id, f"attempt-{attempt_no:02d}-context.json", {
                "hash": context_bundle.content_hash, "manifest": context_bundle.manifest,
            })
            attempt_id = self.db.begin_attempt(
                job.id, attempt_no, candidate, decision.mode, decision.score,
                version_snapshot=version_snapshot, context_hash=context_bundle.content_hash,
                context_manifest=context_bundle.manifest,
            )
            resource_key = candidate.resource_group or f"candidate:{candidate.name}"
            resource_state = {
                "machine": socket.gethostname(), "resource_group": candidate.resource_group,
                "executor": candidate.executor,
                **resource_snapshot(job.worktree),
            }
            if not self.db.reserve_resource(resource_key, job.id, attempt_id, self.owner,
                                            self.cfg.lease_ttl_seconds, resource_state):
                result = ExecutionResult(False, error=f"resource unavailable: {resource_key}")
                self.db.finish_attempt(attempt_id, AttemptStatus.EXECUTOR_FAILED.value, result, 0,
                                       outcome="RESOURCE_FAILURE")
                self.db.reconcile_quota(reservation_id, provider=candidate.quota_group or candidate.executor,
                                        candidate=candidate.name, actual_tokens=0, cost_usd=0, payload={})
                self.db.set_state(job.id, JobState.RETRY)
                total_attempts += 1
                continue
            started = time.monotonic()
            try:
                result = build_executor(candidate.executor).run(req)
            except Exception as exc:
                result = ExecutionResult(False, error=f"executor crash: {type(exc).__name__}: {exc}")
            duration = time.monotonic() - started
            self.db.release_resource(resource_key, job.id)
            self.db.reconcile_quota(
                reservation_id, provider=candidate.quota_group or candidate.base_url or candidate.executor,
                candidate=candidate.name,
                actual_tokens=(result.input_tokens or 0) + (result.output_tokens or 0),
                cost_usd=result.cost_usd, payload=result.raw_metrics,
            )
            provider_status = result.raw_metrics.get("provider_status")
            if provider_status:
                event_type = "RATE_LIMIT" if int(provider_status) == 429 else "PROVIDER_FAILURE"
                self.db.record_quota_event(candidate.provider or candidate.quota_group or candidate.executor,
                                           candidate.name, event_type,
                                           result.raw_metrics.get("rate_headers", {}))
                self.db.event(event_type, job_id=job.id, attempt_id=attempt_id,
                              payload=result.raw_metrics)
            self.reporter.record_attempt(job.id, attempt_no, candidate, decision, result)
            total_attempts += 1
            if not result.ok:
                outcome = "PROVIDER_FAILURE" if provider_status else "EXECUTOR_FAILURE"
                self.db.finish_attempt(attempt_id, AttemptStatus.EXECUTOR_FAILED.value, result, duration,
                                       outcome=outcome)
                failures.append(f"Attempt {attempt_no} executor failure ({candidate.name}): {result.error}")
                self.db.set_state(job.id, JobState.RETRY)
                continue

            # Agents may have committed despite instructions. Preserve content, restore controller Git ownership.
            self.worktrees.normalize_worker_commits(job.worktree, job.baseline_commit)
            self.db.set_state(job.id, JobState.VERIFYING)
            self.db.event("VERIFICATION_STARTED", job_id=job.id, attempt_id=attempt_id)
            v = verify(job, job.worktree, repo_cfg, self.cfg.verifier_sandbox)
            self.db.record_verification(attempt_id, v)
            self.reporter.record_verification(job.id, attempt_no, v)
            if not v.ok:
                result.summary = (result.summary + "\nVerification failed").strip()
                outcome = "TEST_FAILURE" if any(not c.ok and c.name == "test" for c in v.commands) else "VERIFICATION_FAILURE"
                if not v.secret_scan_ok:
                    outcome = "SECURITY_FAILURE"
                elif not v.scope_ok or not v.protected_paths_ok:
                    outcome = "SCOPE_FAILURE"
                self.db.finish_attempt(attempt_id, AttemptStatus.VERIFY_FAILED.value, result, duration,
                                       outcome=outcome)
                self.db.event("VERIFICATION_FAILED", job_id=job.id, attempt_id=attempt_id,
                              payload={"outcome": outcome, "failure": v.short_failure()})
                failures.append(f"Attempt {attempt_no} verification:\n{v.short_failure()}")
                if outcome == "SECURITY_FAILURE":
                    self.db.set_state(job.id, JobState.BLOCKED)
                    self._write_event_audit(job.id)
                    return JobState.BLOCKED
                if outcome == "SCOPE_FAILURE":
                    self.worktrees.reset_attempt(job.worktree, job.baseline_commit)
                self.db.set_state(job.id, JobState.RETRY)
                continue

            if self.cfg.review_enabled:
                self.db.set_state(job.id, JobState.REVIEWING)
                reviewer_pool = [c for c in self.cfg.candidates if c.role == "reviewer" and c.name != candidate.name]
                if reviewer_pool:
                    review_decision = self.scheduler.choose(
                        job, reviewer_pool, role="reviewer", attempt_no=attempt_no
                    )
                    review = ReviewerService().review(
                        job=job,
                        worktree=job.worktree,
                        candidate=review_decision.candidate,
                        attempt_no=attempt_no,
                        verification_summary=self._verification_summary(v),
                    )
                    self.db.record_review(attempt_id, review_decision.candidate.name, review)
                    self.reporter.record_review(job.id, attempt_no, review)
                    if not review.ok:
                        self.db.finish_attempt(attempt_id, AttemptStatus.REVIEW_FAILED.value, result, duration)
                        failures.append(
                            f"Attempt {attempt_no} independent review rejected:\n"
                            + review.summary
                            + "\n"
                            + "\n".join(review.findings)
                        )
                        self.db.set_state(job.id, JobState.RETRY)
                        continue

            self.db.finish_attempt(attempt_id, AttemptStatus.READY.value, result, duration,
                                   outcome="SUCCESS")
            self.db.set_state(job.id, JobState.READY)
            job.state = JobState.READY
            self.reporter.final_diff(job.id, self.worktrees.diff(job.worktree, job.baseline_commit))
            self.reporter.summary(job)
            self._write_event_audit(job.id)
            return JobState.READY

        self.db.set_state(job.id, JobState.FAILED)
        self._write_event_audit(job.id)
        return JobState.FAILED

    def cancel(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if job.state == JobState.ACCEPTED:
            raise RuntimeError("accepted jobs cannot be cancelled")
        self.db.cancel_running_attempts(job_id)
        self.db.set_state(job_id, JobState.CANCELLED)
        self.db.event("JOB_CANCELLED", job_id=job_id)
        job.state = JobState.CANCELLED
        self.reporter.summary(job)

    def cleanup(self, job_id: str, force: bool = False) -> None:
        job = self.db.get_job(job_id)
        safe = {JobState.ACCEPTED, JobState.CANCELLED, JobState.FAILED}
        if job.state not in safe and not force:
            raise RuntimeError(f"refusing to remove {job.state} worktree without --force")
        if job.worktree and job.worktree.exists():
            self.worktrees.destroy(job.repo, job.worktree, force=True)
            job.worktree = None
            self.db.update_job(job)
            self.reporter.record_job(job)

    def accept(self, job_id: str, message: str | None = None) -> str:
        job = self.db.get_job(job_id)
        if job.state != JobState.READY:
            raise RuntimeError(f"{job.id} must be READY_FOR_REVIEW, not {job.state}")
        if not job.worktree:
            raise RuntimeError("job has no worktree")
        commit = self.worktrees.commit(job.worktree, message or f"{job.id}: {job.request[:60]}")
        self.db.add_feedback(
            job.id, "ACCEPT", None, attempt_id=self.db.latest_ready_attempt_id(job.id)
        )
        self.db.event("HUMAN_ACCEPTED", job_id=job.id,
                      attempt_id=self.db.latest_ready_attempt_id(job.id))
        self.db.set_accepted_commit(job.id, commit)
        job.state = JobState.ACCEPTED
        self.reporter.summary(job, commit=commit)
        self.db.event("COMMIT_CREATED", job_id=job.id,
                      attempt_id=self.db.latest_ready_attempt_id(job.id), payload={"commit": commit})
        self._write_event_audit(job.id)
        return commit

    def reject(self, job_id: str, feedback: str) -> None:
        job = self.db.get_job(job_id)
        if job.state not in {JobState.READY, JobState.FAILED, JobState.RETRY}:
            raise RuntimeError(f"cannot reject job in state {job.state}")
        self.db.add_feedback(
            job.id, "REJECT", feedback, attempt_id=self.db.latest_ready_attempt_id(job.id)
        )
        self.db.event("HUMAN_REJECTED", job_id=job.id,
                      attempt_id=self.db.latest_ready_attempt_id(job.id), payload={"feedback": feedback})
        self.db.set_state(job.id, JobState.RETRY)
        job.state = JobState.RETRY
        self.reporter.text(job.id, "latest-human-feedback.txt", feedback + "\n")
        self.reporter.summary(job)
        self._write_event_audit(job.id)
