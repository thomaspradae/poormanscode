from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import asdict

from .accounting import ModelRequestAccounting
from .capabilities import CapabilityRegistry
from .capabilities import infer_required_capabilities, repository_is_skeletal
from .config import PMCConfig, load_repo_config_at
from .context import build_context_bundle
from .db import Database
from .domain import AttemptStatus, ExecutionRequest, ExecutionResult, JobState, Outcome
from .executors import build_executor
from .gitops import WorktreeManager
from .prompts import builder_prompt
from .reporting import Reporter
from .research import ResearchService
from .retry import RetryAction, policy_for
from .reviewer import ReviewerService
from .sandbox import resource_snapshot
from .scheduler import NoAvailableCandidate, Scheduler
from .verifier import verify
from .versioning import (
    JOB_CONTRACT_VERSION,
    PROMPT_PROFILE_VERSION,
    SCHEMA_VERSION,
    TOOLCHAIN_PROFILE_VERSION,
    VERIFIER_VERSION,
    pmc_git_sha,
    stable_hash,
)


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

        self.thread = threading.Thread(
            target=beat, daemon=True, name=f"pmc-lease-{self.job_id}"
        )
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.db.release_lease(self.job_id, self.owner)


class _ResourceHeartbeat:
    def __init__(
        self, db: Database, resource_key: str, job_id: str, owner: str, ttl: int
    ):
        self.db = db
        self.resource_key = resource_key
        self.job_id = job_id
        self.owner = owner
        self.ttl = ttl
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self):
        interval = max(5.0, self.ttl / 3)

        def beat():
            while not self.stop.wait(interval):
                if not self.db.renew_resource(
                    self.resource_key, self.job_id, self.owner, self.ttl
                ):
                    return

        self.thread = threading.Thread(
            target=beat, daemon=True, name=f"pmc-resource-{self.job_id}"
        )
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.db.release_resource(self.resource_key, self.job_id)


class Controller:
    def __init__(self, cfg: PMCConfig):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.worktrees = WorktreeManager(cfg.worktrees_dir)
        self.capabilities = CapabilityRegistry(self.db, cfg.toolchains)
        self.scheduler = Scheduler(
            self.db,
            cfg.exploration_rate,
            cfg.min_samples_per_candidate,
            self.capabilities,
        )
        self.reporter = Reporter(cfg.runs_dir, cfg.artifact_max_bytes)
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.db.recover_expired_leases(cfg.max_attempts)

    def _write_event_audit(self, job_id: str) -> None:
        self.reporter.json(job_id, "events.json", self.db.job_events(job_id))

    def ensure_worktree(self, job):
        if job.worktree and job.worktree.exists() and job.baseline_commit:
            return job
        wt, baseline = self.worktrees.create(job.repo, job.id, job.base_branch)
        dependency_commits = self.db.dependency_commits(job.id)
        if dependency_commits:
            from .gitops import git, resolve_commit

            for commit in dependency_commits:
                result = git(wt, "cherry-pick", commit, check=False)
                if result.returncode != 0:
                    git(wt, "cherry-pick", "--abort", check=False)
                    raise RuntimeError(
                        f"dependency integration conflict at {commit}: {result.stderr.strip()}"
                    )
            baseline = resolve_commit(wt, "HEAD")
            self.db.event(
                "DEPENDENCIES_INTEGRATED",
                job_id=job.id,
                payload={"commits": dependency_commits, "baseline": baseline},
            )
        if self.cfg.verifier_sandbox == "restricted-user":
            if not shutil.which("setfacl"):
                raise RuntimeError("restricted-user verifier requires setfacl")
            controller_user = pwd.getpwuid(os.getuid()).pw_name
            subprocess.run(
                [
                    "setfacl",
                    "-Rm",
                    f"u:{controller_user}:rwX,u:pmc-worker:rwX",
                    str(wt),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "setfacl",
                    "-Rm",
                    f"d:u:{controller_user}:rwX,d:u:pmc-worker:rwX",
                    str(wt),
                ],
                check=True,
            )
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
                    {
                        "name": c.name,
                        "exit_code": c.exit_code,
                        "seconds": round(c.duration_seconds, 2),
                    }
                    for c in v.commands
                ],
            },
            indent=2,
        )

    def run_job(self, job_id: str, forced_candidate: str | None = None) -> JobState:
        with _LeaseHeartbeat(self.db, job_id, self.owner, self.cfg.lease_ttl_seconds):
            return self._run_job_locked(job_id, forced_candidate)

    def _run_job_locked(
        self, job_id: str, forced_candidate: str | None = None
    ) -> JobState:
        job = self.db.get_job(job_id)
        if "required_capabilities" not in job.constraints:
            inferred = infer_required_capabilities(
                job.request, skeletal=repository_is_skeletal(job.repo)
            )
            if inferred:
                job.constraints["required_capabilities"] = inferred
                self.db.update_job(job)
                self.db.event(
                    "CAPABILITIES_INFERRED",
                    job_id=job.id,
                    payload={"required": inferred, "source": "runtime-preflight"},
                )
        job = self.ensure_worktree(job)
        if job.state in {JobState.ACCEPTED, JobState.CANCELLED}:
            raise RuntimeError(f"{job.id} is {job.state}")
        repo_cfg = load_repo_config_at(job.repo, job.baseline_commit)
        if repo_cfg.get("toolchain") == "unity":
            # Machine-owned registration is injected only after immutable policy
            # loading; workers cannot redirect the verifier executable.
            repo_cfg["unity_toolchain"] = dict(self.cfg.toolchains.get("unity", {}))
        prior_feedback = self.db.feedback_text(job.id)
        total_attempts = self.db.attempt_count(job.id)
        failures: list[str] = []
        used: list[str] = []
        retry_exclude: set[str] = set()
        retry_same: str | None = None

        while total_attempts < self.cfg.max_attempts:
            attempt_no = total_attempts + 1
            exclude = set(retry_exclude)
            retry_exclude.clear()
            if used and len(used) >= self.cfg.same_candidate_retries:
                exclude.add(used[-1])
            persisted_decision = self.db.scheduler_decision(job.id, attempt_no)
            if persisted_decision:
                from .domain import SchedulerDecision

                matches = [
                    c
                    for c in self.cfg.candidates
                    if c.name == persisted_decision["candidate"] and c.enabled
                ]
                if not matches:
                    raise RuntimeError(
                        f"persisted candidate unavailable: {persisted_decision['candidate']}"
                    )
                candidate0 = matches[0]
                decision = SchedulerDecision(
                    candidate0,
                    persisted_decision["mode"],
                    persisted_decision["score"],
                    persisted_decision["reason"],
                    json.loads(persisted_decision.get("eligible_json") or "[]"),
                    json.loads(persisted_decision.get("unavailable_json") or "{}"),
                    persisted_decision.get("selection_probability") or 0.0,
                    persisted_decision.get("policy_version") or "unknown",
                    json.loads(persisted_decision.get("snapshot_json") or "{}"),
                )
            elif (forced_candidate and total_attempts == 0) or retry_same:
                selected_name = forced_candidate if total_attempts == 0 else retry_same
                retry_same = None
                matches = [
                    c
                    for c in self.cfg.candidates
                    if c.name == selected_name and c.enabled
                ]
                if not matches:
                    raise RuntimeError(
                        f"forced candidate is unavailable: {selected_name}"
                    )
                from .domain import SchedulerDecision

                candidate0 = matches[0]
                mode = (
                    "forced"
                    if forced_candidate and total_attempts == 0
                    else "retry_same"
                )
                decision = SchedulerDecision(candidate0, mode, 0.0, mode)
            else:
                try:
                    decision = self.scheduler.choose(
                        job,
                        self.cfg.candidates,
                        role="builder",
                        exclude=exclude,
                        attempt_no=attempt_no,
                    )
                except NoAvailableCandidate:
                    # If excluding the last worker emptied the pool, allow it again.
                    try:
                        decision = self.scheduler.choose(
                            job,
                            self.cfg.candidates,
                            role="builder",
                            attempt_no=attempt_no,
                        )
                    except NoAvailableCandidate as exc:
                        missing = {
                            name: reason
                            for name, reason in exc.unavailable.items()
                            if reason.startswith("missing capabilities:")
                        }
                        if missing:
                            self.db.event(
                                "BLOCKED_CAPABILITY",
                                job_id=job.id,
                                payload={
                                    "required": job.constraints.get(
                                        "required_capabilities", []
                                    ),
                                    "candidates": exc.unavailable,
                                },
                            )
                            self.db.set_state(job.id, JobState.BLOCKED)
                            self._write_event_audit(job.id)
                            return JobState.BLOCKED
                        raise
            if not persisted_decision:
                self.db.record_decision(job.id, attempt_no, decision)
            candidate = decision.candidate
            self.db.register_candidate(candidate)
            used.append(candidate.name)
            # Bash accounts for every model turn. Other adapters retain a single
            # attempt-level reservation until their APIs expose turn boundaries.
            reservation_id = None
            if candidate.executor != "bash":
                reservation_id = self.db.reserve_quota(
                    candidate.name, job.id, int(candidate.extra.get("max_tokens", 4096))
                )
            prompt_feedback = "\n\n".join(x for x in [prior_feedback, *failures] if x)
            context_bundle = build_context_bundle(
                job.worktree, job.request, baseline=job.baseline_commit
            )
            prompt = builder_prompt(
                job,
                job.worktree,
                repo_cfg,
                prompt_feedback,
                context=context_bundle.content,
            )
            req = ExecutionRequest(
                job,
                candidate,
                job.worktree,
                prompt,
                attempt_no,
                prompt_feedback or None,
            )
            self.reporter.text(
                job.id, f"attempt-{attempt_no:02d}-prompt.txt", prompt + "\n"
            )
            self.db.set_state(job.id, JobState.RUNNING)
            version_snapshot = {
                "pmc_git_sha": pmc_git_sha(),
                "schema_version": SCHEMA_VERSION,
                "config_hash": stable_hash(asdict(self.cfg)),
                "job_contract_version": JOB_CONTRACT_VERSION,
                "prompt_profile_version": PROMPT_PROFILE_VERSION,
                "context_builder_version": context_bundle.manifest["version"],
                "verifier_version": VERIFIER_VERSION,
                "toolchain_profile_version": TOOLCHAIN_PROFILE_VERSION,
                "toolchain": repo_cfg.get("toolchain"),
                "toolchain_hash": stable_hash(repo_cfg.get("unity_toolchain", {})),
                "candidate_id": candidate.name,
                "candidate_version": candidate.version,
                "candidate_hash": stable_hash(candidate),
                "executor": candidate.executor,
                "executor_version": "1",
                "provider": candidate.provider or candidate.quota_group,
                "provider_model_id": candidate.model,
                "prompt_profile": candidate.prompt_profile,
                "tool_profile": candidate.tool_profile,
                "resource_class": candidate.resource_class,
                "base_repository_sha": job.baseline_commit,
                "verification_policy_hash": stable_hash(repo_cfg),
            }
            self.reporter.json(
                job.id,
                f"attempt-{attempt_no:02d}-context.json",
                {
                    "hash": context_bundle.content_hash,
                    "manifest": context_bundle.manifest,
                },
            )
            attempt_id = self.db.begin_attempt(
                job.id,
                attempt_no,
                candidate,
                decision.mode,
                decision.score,
                version_snapshot=version_snapshot,
                context_hash=context_bundle.content_hash,
                context_manifest=context_bundle.manifest,
            )
            if candidate.executor == "bash":
                req.accounting = ModelRequestAccounting(
                    self.db, job_id=job.id, attempt_id=attempt_id, candidate=candidate
                )
                if self.cfg.research_enabled:
                    req.research = ResearchService(
                        self.db,
                        job_id=job.id,
                        attempt_id=attempt_id,
                        model=self.cfg.research_model,
                        api_key_env=self.cfg.research_api_key_env,
                        max_queries=self.cfg.research_max_queries_per_attempt,
                    )
            resource_key = candidate.resource_group or f"candidate:{candidate.name}"
            resource_state = {
                "machine": socket.gethostname(),
                "resource_group": candidate.resource_group,
                "executor": candidate.executor,
                "requested": candidate.extra.get("resource_requirements", {}),
                "capacity": candidate.extra.get("resource_capacity", {}),
                **resource_snapshot(job.worktree),
            }
            if not self.db.reserve_resource(
                resource_key,
                job.id,
                attempt_id,
                self.owner,
                self.cfg.lease_ttl_seconds,
                resource_state,
            ):
                result = ExecutionResult(
                    False, error=f"resource unavailable: {resource_key}"
                )
                self.db.finish_attempt(
                    attempt_id,
                    AttemptStatus.EXECUTOR_FAILED.value,
                    result,
                    0,
                    outcome="RESOURCE_FAILURE",
                )
                if reservation_id:
                    self.db.reconcile_quota(
                        reservation_id,
                        provider=candidate.quota_group or candidate.executor,
                        candidate=candidate.name,
                        actual_tokens=0,
                        cost_usd=0,
                        payload={},
                    )
                self.db.set_state(job.id, JobState.RETRY)
                total_attempts += 1
                continue
            started = time.monotonic()
            with _ResourceHeartbeat(
                self.db,
                resource_key,
                job.id,
                self.owner,
                self.cfg.lease_ttl_seconds,
            ):
                try:
                    result = build_executor(candidate.executor).run(req)
                except Exception as exc:
                    result = ExecutionResult(
                        False,
                        error=f"executor crash: {type(exc).__name__}: {exc}",
                        outcome=Outcome.EXECUTOR_CRASH,
                    )
            duration = time.monotonic() - started
            result.accounting_level = {
                "bash": "per_model_request",
                "openhands": "aggregate",
                "jules": "unknown",
            }.get(candidate.executor, "unknown")
            result.raw_metrics.setdefault("accounting", result.accounting_level)
            if reservation_id:
                self.db.reconcile_quota(
                    reservation_id,
                    provider=candidate.quota_group
                    or candidate.base_url
                    or candidate.executor,
                    candidate=candidate.name,
                    actual_tokens=(result.input_tokens or 0)
                    + (result.output_tokens or 0),
                    cost_usd=result.cost_usd,
                    payload=result.raw_metrics,
                )
            elif candidate.executor == "bash":
                totals = self.db.model_request_totals(attempt_id)
                result.raw_metrics["model_request_totals"] = totals
                recorded = totals["input_tokens"] + totals["output_tokens"]
                reported = (result.input_tokens or 0) + (result.output_tokens or 0)
                cost_mismatch = (
                    totals["cost_usd"] is not None
                    and abs(float(totals["cost_usd"]) - float(result.cost_usd or 0))
                    > 1e-9
                )
                if recorded != reported or cost_mismatch:
                    result.ok = False
                    result.error = (
                        f"accounting mismatch: model request tokens={recorded}, "
                        f"executor tokens={reported}, model request cost={totals['cost_usd']}, "
                        f"executor cost={result.cost_usd}"
                    )
                    result.outcome = Outcome.POLICY_FAILURE
            provider_status = result.raw_metrics.get("provider_status")
            if provider_status:
                event_type = (
                    "RATE_LIMIT" if int(provider_status) == 429 else "PROVIDER_FAILURE"
                )
                self.db.record_quota_event(
                    candidate.provider or candidate.quota_group or candidate.executor,
                    candidate.name,
                    event_type,
                    result.raw_metrics.get("rate_headers", {}),
                )
                self.db.event(
                    event_type,
                    job_id=job.id,
                    attempt_id=attempt_id,
                    payload=result.raw_metrics,
                )
            self.reporter.record_attempt(
                job.id, attempt_no, candidate, decision, result
            )
            total_attempts += 1
            if not result.ok:
                outcome = result.outcome or (
                    Outcome.PROVIDER_FAILURE
                    if provider_status
                    else Outcome.EXECUTOR_FAILURE
                )
                self.db.finish_attempt(
                    attempt_id,
                    AttemptStatus.EXECUTOR_FAILED.value,
                    result,
                    duration,
                    outcome=outcome.value if isinstance(outcome, Outcome) else outcome,
                )
                failures.append(
                    f"Attempt {attempt_no} executor failure ({candidate.name}): {result.error}"
                )
                retry_policy = policy_for(outcome)
                if retry_policy.action == RetryAction.BLOCK:
                    self.db.set_state(job.id, JobState.BLOCKED)
                    self._write_event_audit(job.id)
                    return JobState.BLOCKED
                if retry_policy.action == RetryAction.TERMINAL:
                    self.db.set_state(job.id, JobState.FAILED)
                    self._write_event_audit(job.id)
                    return JobState.FAILED
                if outcome == Outcome.RESOURCE_FAILURE:
                    self.worktrees.reset_attempt(job.worktree, job.baseline_commit)
                if job.constraints.get("_candidate_order"):
                    retry_same = None
                    retry_exclude.add(candidate.name)
                elif retry_policy.action == RetryAction.RETRY_ALTERNATE:
                    retry_exclude.add(candidate.name)
                elif retry_policy.action == RetryAction.RETRY_SAME_WITH_EVIDENCE:
                    if used.count(candidate.name) <= self.cfg.same_candidate_retries:
                        retry_same = candidate.name
                    else:
                        retry_exclude.add(candidate.name)
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
                failed_commands = {c.name.lower() for c in v.commands if not c.ok}
                outcome = next(
                    (
                        x
                        for name, x in (
                            ("test", Outcome.TEST_FAILURE),
                            ("lint", Outcome.LINT_FAILURE),
                            ("build", Outcome.BUILD_FAILURE),
                            ("typecheck", Outcome.TYPECHECK_FAILURE),
                        )
                        if name in failed_commands
                    ),
                    Outcome.POLICY_FAILURE,
                )
                if not v.secret_scan_ok:
                    outcome = Outcome.SECURITY_FAILURE
                elif not v.scope_ok or not v.protected_paths_ok:
                    outcome = Outcome.SCOPE_FAILURE
                elif not v.dependencies_ok:
                    outcome = Outcome.POLICY_FAILURE
                self.db.finish_attempt(
                    attempt_id,
                    AttemptStatus.VERIFY_FAILED.value,
                    result,
                    duration,
                    outcome=outcome.value,
                )
                self.db.event(
                    "VERIFICATION_FAILED",
                    job_id=job.id,
                    attempt_id=attempt_id,
                    payload={"outcome": outcome.value, "failure": v.short_failure()},
                )
                failures.append(
                    f"Attempt {attempt_no} verification:\n{v.short_failure()}"
                )
                retry_policy = policy_for(outcome)
                if retry_policy.action == RetryAction.BLOCK:
                    self.db.set_state(job.id, JobState.BLOCKED)
                    self._write_event_audit(job.id)
                    return JobState.BLOCKED
                if outcome == Outcome.SCOPE_FAILURE:
                    self.worktrees.reset_attempt(job.worktree, job.baseline_commit)
                if job.constraints.get("_candidate_order"):
                    retry_same = None
                    retry_exclude.add(candidate.name)
                elif retry_policy.action == RetryAction.RETRY_ALTERNATE:
                    retry_exclude.add(candidate.name)
                elif retry_policy.action == RetryAction.RETRY_SAME_WITH_EVIDENCE:
                    if used.count(candidate.name) <= self.cfg.same_candidate_retries:
                        retry_same = candidate.name
                    else:
                        retry_exclude.add(candidate.name)
                self.db.set_state(job.id, JobState.RETRY)
                continue

            if self.cfg.review_enabled:
                self.db.set_state(job.id, JobState.REVIEWING)
                reviewer_pool = [
                    c
                    for c in self.cfg.candidates
                    if c.role == "reviewer" and c.name != candidate.name
                ]
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
                    self.db.record_review(
                        attempt_id, review_decision.candidate.name, review
                    )
                    self.reporter.record_review(job.id, attempt_no, review)
                    if not review.ok:
                        self.db.finish_attempt(
                            attempt_id,
                            AttemptStatus.REVIEW_FAILED.value,
                            result,
                            duration,
                            outcome=Outcome.REVIEW_FAILURE.value,
                        )
                        failures.append(
                            f"Attempt {attempt_no} independent review rejected:\n"
                            + review.summary
                            + "\n"
                            + "\n".join(review.findings)
                        )
                        if (
                            used.count(candidate.name)
                            <= self.cfg.same_candidate_retries
                        ):
                            retry_same = candidate.name
                        else:
                            retry_exclude.add(candidate.name)
                        self.db.set_state(job.id, JobState.RETRY)
                        continue

            self.db.finish_attempt(
                attempt_id,
                AttemptStatus.READY.value,
                result,
                duration,
                outcome="SUCCESS",
            )
            self.db.set_state(job.id, JobState.READY)
            job.state = JobState.READY
            self.reporter.final_diff(
                job.id, self.worktrees.diff(job.worktree, job.baseline_commit)
            )
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
            raise RuntimeError(
                f"refusing to remove {job.state} worktree without --force"
            )
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
        commit = self.worktrees.commit_idempotent(
            job.worktree,
            job.baseline_commit or "HEAD^",
            message or f"{job.id}: {job.request[:60]}",
            job.id,
        )
        ready_attempt = self.db.latest_ready_attempt_id(job.id)
        if ready_attempt is None:
            raise RuntimeError("job has no READY attempt to accept")
        self.db.complete_acceptance(job.id, ready_attempt, commit)
        job.state = JobState.ACCEPTED
        self.reporter.summary(job, commit=commit)
        self._write_event_audit(job.id)
        return commit

    def reject(self, job_id: str, feedback: str) -> None:
        job = self.db.get_job(job_id)
        if job.state not in {JobState.READY, JobState.FAILED, JobState.RETRY}:
            raise RuntimeError(f"cannot reject job in state {job.state}")
        self.db.add_feedback(
            job.id,
            "REJECT",
            feedback,
            attempt_id=self.db.latest_ready_attempt_id(job.id),
        )
        self.db.event(
            "HUMAN_REJECTED",
            job_id=job.id,
            attempt_id=self.db.latest_ready_attempt_id(job.id),
            payload={"feedback": feedback},
        )
        self.db.set_state(job.id, JobState.RETRY)
        job.state = JobState.RETRY
        self.reporter.text(job.id, "latest-human-feedback.txt", feedback + "\n")
        self.reporter.summary(job)
        self._write_event_audit(job.id)
