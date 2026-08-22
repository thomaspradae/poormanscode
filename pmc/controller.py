from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

from .config import PMCConfig, load_repo_config
from .db import Database
from .domain import AttemptStatus, ExecutionRequest, ExecutionResult, JobState
from .executors import build_executor
from .gitops import WorktreeManager
from .prompts import builder_prompt
from .reviewer import ReviewerService
from .reporting import Reporter
from .scheduler import Scheduler
from .verifier import verify


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

    def ensure_worktree(self, job):
        if job.worktree and job.worktree.exists() and job.baseline_commit:
            return job
        wt, baseline = self.worktrees.create(job.repo, job.id, job.base_branch)
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
        repo_cfg = load_repo_config(job.worktree)
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
            used.append(candidate.name)
            prompt_feedback = "\n\n".join(x for x in [prior_feedback, *failures] if x)
            prompt = builder_prompt(job, job.worktree, repo_cfg, prompt_feedback)
            req = ExecutionRequest(job, candidate, job.worktree, prompt, attempt_no, prompt_feedback or None)
            self.reporter.text(job.id, f"attempt-{attempt_no:02d}-prompt.txt", prompt + "\n")
            self.db.set_state(job.id, JobState.RUNNING)
            attempt_id = self.db.begin_attempt(
                job.id, attempt_no, candidate, decision.mode, decision.score
            )
            started = time.monotonic()
            try:
                result = build_executor(candidate.executor).run(req)
            except Exception as exc:
                result = ExecutionResult(False, error=f"executor crash: {type(exc).__name__}: {exc}")
            duration = time.monotonic() - started
            self.reporter.record_attempt(job.id, attempt_no, candidate, decision, result)
            total_attempts += 1
            if not result.ok:
                self.db.finish_attempt(attempt_id, AttemptStatus.EXECUTOR_FAILED.value, result, duration)
                failures.append(f"Attempt {attempt_no} executor failure ({candidate.name}): {result.error}")
                self.db.set_state(job.id, JobState.RETRY)
                continue

            # Agents may have committed despite instructions. Preserve content, restore controller Git ownership.
            self.worktrees.normalize_worker_commits(job.worktree, job.baseline_commit)
            self.db.set_state(job.id, JobState.VERIFYING)
            v = verify(job, job.worktree, repo_cfg)
            self.db.record_verification(attempt_id, v)
            self.reporter.record_verification(job.id, attempt_no, v)
            if not v.ok:
                result.summary = (result.summary + "\nVerification failed").strip()
                self.db.finish_attempt(attempt_id, AttemptStatus.VERIFY_FAILED.value, result, duration)
                failures.append(f"Attempt {attempt_no} verification:\n{v.short_failure()}")
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

            self.db.finish_attempt(attempt_id, AttemptStatus.READY.value, result, duration)
            self.db.set_state(job.id, JobState.READY)
            job.state = JobState.READY
            self.reporter.final_diff(job.id, self.worktrees.diff(job.worktree, job.baseline_commit))
            self.reporter.summary(job)
            return JobState.READY

        self.db.set_state(job.id, JobState.FAILED)
        return JobState.FAILED

    def cancel(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if job.state == JobState.ACCEPTED:
            raise RuntimeError("accepted jobs cannot be cancelled")
        self.db.set_state(job_id, JobState.CANCELLED)
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
        self.db.set_accepted_commit(job.id, commit)
        job.state = JobState.ACCEPTED
        self.reporter.summary(job, commit=commit)
        return commit

    def reject(self, job_id: str, feedback: str) -> None:
        job = self.db.get_job(job_id)
        if job.state not in {JobState.READY, JobState.FAILED, JobState.RETRY}:
            raise RuntimeError(f"cannot reject job in state {job.state}")
        self.db.add_feedback(
            job.id, "REJECT", feedback, attempt_id=self.db.latest_ready_attempt_id(job.id)
        )
        self.db.set_state(job.id, JobState.RETRY)
        job.state = JobState.RETRY
        self.reporter.text(job.id, "latest-human-feedback.txt", feedback + "\n")
        self.reporter.summary(job)
