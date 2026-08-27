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
from dataclasses import asdict, replace

from .accounting import ModelRequestAccounting
from .capabilities import (
    CapabilityRegistry,
    infer_required_capabilities,
    repository_is_skeletal,
)
from .config import PMCConfig, load_repo_config_at
from .context import build_context_bundle
from .db import Database
from .domain import AttemptStatus, ExecutionRequest, ExecutionResult, JobState, Outcome
from .executors import build_executor
from .gitops import WorktreeManager
from .intelligence import advisory_call, allocate_intelligence
from .prompts import builder_prompt
from .reporting import Reporter
from .research import ResearchService
from .retry import RetryAction, policy_for
from .reviewer import ReviewerService
from .sandbox import resource_snapshot
from .scheduler import NoAvailableCandidate, Scheduler
from .verifier import select_verifier_runtime, verify
from .versioning import (
    JOB_CONTRACT_VERSION,
    PROMPT_PROFILE_VERSION,
    SCHEMA_VERSION,
    TOOLCHAIN_PROFILE_VERSION,
    VERIFIER_VERSION,
    executor_adapter_version,
    pmc_git_sha,
    stable_hash,
)
from .work_state import WORK_STATE_VERSION, WorkState, build_work_state


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
        self.db.register_provider_credentials(
            [
                credential
                for pool in cfg.provider_credentials.values()
                for credential in pool
            ]
        )
        self.worktrees = WorktreeManager(cfg.worktrees_dir)
        self.capabilities = CapabilityRegistry(self.db, cfg.toolchains)
        self.scheduler = Scheduler(
            self.db,
            cfg.exploration_rate,
            cfg.min_samples_per_candidate,
            self.capabilities,
            cfg.require_model_conformance,
            cfg.router_policy,
            cfg.contextual_min_observations,
            cfg.bandit_simulations,
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

            already_present: list[str] = []
            for commit in dependency_commits:
                if (
                    git(
                        wt,
                        "merge-base",
                        "--is-ancestor",
                        commit,
                        "HEAD",
                        check=False,
                    ).returncode
                    == 0
                ):
                    already_present.append(commit)
                    continue
                cherry = git(wt, "cherry", "HEAD", commit, check=False)
                target_patch_state = {
                    parts[1]: parts[0]
                    for line in cherry.stdout.splitlines()
                    if len(parts := line.split(maxsplit=1)) == 2
                }.get(commit)
                if cherry.returncode == 0 and target_patch_state == "-":
                    # The accepted commit may have been human-integrated via
                    # cherry-pick and therefore have a different SHA but the
                    # same patch identity.
                    already_present.append(commit)
                    continue
                result = git(wt, "cherry-pick", commit, check=False)
                if result.returncode != 0:
                    git(wt, "cherry-pick", "--abort", check=False)
                    # A verified checkpoint may have been created from the
                    # original baseline while an earlier dependency added a
                    # narrow follow-up fix. Preserve the current dependency
                    # on overlapping hunks, then apply non-conflicting files.
                    parent = git(wt, "rev-parse", f"{commit}^", check=False)
                    parent_is_ancestor = (
                        parent.returncode == 0
                        and git(
                            wt,
                            "merge-base",
                            "--is-ancestor",
                            parent.stdout.strip(),
                            "HEAD",
                            check=False,
                        ).returncode
                        == 0
                    )
                    if parent_is_ancestor:
                        retry = git(wt, "cherry-pick", "-X", "ours", commit, check=False)
                        if retry.returncode == 0:
                            continue
                        git(wt, "cherry-pick", "--abort", check=False)
                    raise RuntimeError(
                        f"dependency integration conflict at {commit}: {result.stderr.strip()}"
                    )
            baseline = resolve_commit(wt, "HEAD")
            self.db.event(
                "DEPENDENCIES_INTEGRATED",
                job_id=job.id,
                payload={
                    "commits": dependency_commits,
                    "already_present": already_present,
                    "baseline": baseline,
                },
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

    def _capture_work_state(
        self,
        job,
        attempt_no: int,
        failures: list[str],
        current_plan: list[str] | None = None,
    ) -> WorkState:
        state = build_work_state(
            job,
            job.worktree,
            failures=failures,
            current_plan=current_plan,
        )
        self.reporter.json(
            job.id,
            f"attempt-{attempt_no:02d}-work-state.json",
            {"manifest": state.manifest(), "state": state},
        )
        self.db.event(
            "WORK_STATE_CAPTURED",
            job_id=job.id,
            payload={"attempt_no": attempt_no, **state.manifest()},
        )
        return state

    def run_job(self, job_id: str, forced_candidate: str | None = None) -> JobState:
        with _LeaseHeartbeat(self.db, job_id, self.owner, self.cfg.lease_ttl_seconds):
            return self._run_job_locked(job_id, forced_candidate)

    def reverify_job(self, job_id: str) -> JobState:
        """Re-run acceptance gates for the latest produced patch without recoding."""
        job = self.db.get_job(job_id)
        if not job.worktree or not job.worktree.exists() or not job.baseline_commit:
            raise RuntimeError(f"{job.id} has no preserved worktree to re-verify")
        detail = self.db.job_detail(job_id)
        attempts = detail["attempts"]
        if not attempts:
            raise RuntimeError(f"{job.id} has no attempt to re-verify")
        attempt = attempts[-1]
        candidate = next(
            (c for c in self.cfg.candidates if c.name == attempt["candidate"]), None
        )
        if candidate is None:
            raise RuntimeError(
                f"candidate is no longer configured: {attempt['candidate']}"
            )
        repo_cfg = load_repo_config_at(job.repo, job.baseline_commit)
        if repo_cfg.get("toolchain") == "unity":
            repo_cfg["unity_toolchain"] = dict(self.cfg.toolchains.get("unity", {}))
        sandbox, verifier_config, source = select_verifier_runtime(
            repo_cfg,
            self.cfg.toolchains,
            candidate.extra,
            self.cfg.verifier_sandbox,
        )
        attempt_id = int(attempt["id"])
        self.db.event(
            "REVERIFICATION_STARTED",
            job_id=job.id,
            attempt_id=attempt_id,
            payload={
                "sandbox": sandbox,
                "source": source,
                "toolchain": repo_cfg.get("toolchain"),
                "remote_host": verifier_config.get("remote_host"),
                "remote_instance": verifier_config.get("remote_instance"),
            },
        )
        result = verify(
            job,
            job.worktree,
            repo_cfg,
            sandbox,
            verifier_config,
        )
        self.db.record_verification(attempt_id, result)
        self.reporter.record_verification(job.id, int(attempt["attempt_no"]), result)
        if result.ok:
            self.db.mark_attempt_ready_after_reverification(attempt_id)
            self.db.set_state(job.id, JobState.READY)
            self.db.event("REVERIFICATION_PASSED", job_id=job.id, attempt_id=attempt_id)
            self._write_event_audit(job.id)
            return JobState.READY
        self.db.set_state(job.id, JobState.BLOCKED)
        self.db.event(
            "REVERIFICATION_FAILED",
            job_id=job.id,
            attempt_id=attempt_id,
            payload={"failure": result.short_failure()},
        )
        self._write_event_audit(job.id)
        return JobState.BLOCKED

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
        cycle_attempts = self.db.attempt_count_since_latest_feedback(job.id)
        failures: list[str] = []
        current_plan: list[str] = []
        used: list[str] = []
        retry_exclude: set[str] = set()
        retry_same: str | None = None
        contextual_rows = self.db.contextual_candidate_stats(job, "builder", "all")
        observations = sum(int(row["observations"]) for row in contextual_rows)
        verification_strong = bool(repo_cfg.get("test")) and bool(
            repo_cfg.get("build") or repo_cfg.get("lint") or repo_cfg.get("typecheck")
        )
        intelligence_plan = allocate_intelligence(
            job, verification_strong=verification_strong, observations=observations
        )

        run_started = time.monotonic()
        max_attempts = min(job.budget.max_attempts, 1 + job.budget.max_repairs)
        while cycle_attempts < max_attempts:
            if time.monotonic() - run_started >= job.budget.max_wall_seconds:
                failures.append("job wall-clock budget exhausted")
                break
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
            elif (forced_candidate and cycle_attempts == 0) or retry_same:
                selected_name = forced_candidate if cycle_attempts == 0 else retry_same
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
                    if forced_candidate and cycle_attempts == 0
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
                providers = {c.provider for c in self.cfg.candidates if c.provider}
                decision.snapshot.update(
                    {
                        "provider_capacity": {
                            provider: self.db.provider_capacity(provider)
                            for provider in sorted(providers)
                        },
                        "budget": asdict(job.budget),
                        "complexity": job.complexity,
                        "risk": job.risk,
                        "intelligence_plan": asdict(intelligence_plan),
                    }
                )
                self.db.record_decision(job.id, attempt_no, decision)
            candidate = decision.candidate
            self.db.register_candidate(candidate)
            used.append(candidate.name)
            # Bash and built-in OpenHands account for every model turn. ACP and
            # external executors retain one attempt-level reservation because
            # their internal request boundary is outside PMC.
            reservation_id = None
            credential_reservation = None
            request_accounted_executor = candidate.executor == "bash" or (
                candidate.executor == "openhands"
                and candidate.extra.get("agent_kind") != "acp"
            )
            work_state = build_work_state(
                job,
                job.worktree,
                failures=failures,
                current_plan=current_plan,
            )
            prompt_feedback = "\n\n".join(
                x
                for x in [
                    prior_feedback,
                    work_state.prompt_packet()
                    if work_state.has_partial_work or failures
                    else "",
                ]
                if x
            )
            context_bundle = build_context_bundle(
                job.worktree,
                job.request,
                # The work-state packet already carries the current diff.
                # Avoid sending the same patch twice on recovery attempts.
                baseline=(None if work_state.has_partial_work else job.baseline_commit),
                limit=int(candidate.extra.get("context_limit", 24_000)),
            )
            prompt = builder_prompt(
                job,
                job.worktree,
                repo_cfg,
                prompt_feedback,
                context=context_bundle.content,
            )
            attempt_context_manifest = {
                **context_bundle.manifest,
                "work_state": work_state.manifest(),
            }
            attempt_context_hash = stable_hash(
                {
                    "repository_context": context_bundle.content_hash,
                    "work_state": work_state.content_hash,
                }
            )
            execution_candidate = replace(
                candidate,
                api_key_env=(
                    credential_reservation["api_key_env"]
                    if credential_reservation
                    else candidate.api_key_env
                ),
            )
            req = ExecutionRequest(
                job,
                execution_candidate,
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
                "work_state_version": WORK_STATE_VERSION,
                "work_state_hash": work_state.content_hash,
                "verifier_version": VERIFIER_VERSION,
                "toolchain_profile_version": TOOLCHAIN_PROFILE_VERSION,
                "toolchain": repo_cfg.get("toolchain"),
                "toolchain_hash": stable_hash(repo_cfg.get("unity_toolchain", {})),
                "candidate_id": candidate.name,
                "candidate_version": candidate.version,
                "candidate_hash": stable_hash(candidate),
                "executor": candidate.executor,
                "executor_version": executor_adapter_version(candidate.executor),
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
                    "hash": attempt_context_hash,
                    "manifest": attempt_context_manifest,
                },
            )
            attempt_id = self.db.begin_attempt(
                job.id,
                attempt_no,
                candidate,
                decision.mode,
                decision.score,
                version_snapshot=version_snapshot,
                context_hash=attempt_context_hash,
                context_manifest=attempt_context_manifest,
            )
            if not request_accounted_executor:
                if candidate.provider:
                    ok, reason = self.db.provider_availability(candidate.provider)
                    if ok and reason != "legacy candidate credential":
                        credential_reservation = self.db.reserve_provider_credential(
                            candidate.provider,
                            job.id,
                            attempt_id,
                            int(candidate.extra.get("max_tokens", 4096)),
                        )
                reservation_id = self.db.reserve_quota(
                    candidate.name,
                    job.id,
                    int(candidate.extra.get("max_tokens", 4096)),
                    (
                        credential_reservation["credential_id"]
                        if credential_reservation
                        else None
                    ),
                    (
                        credential_reservation["quota_scope_id"]
                        if credential_reservation
                        else None
                    ),
                )
                execution_candidate = replace(
                    candidate,
                    api_key_env=(
                        credential_reservation["api_key_env"]
                        if credential_reservation
                        else candidate.api_key_env
                    ),
                )
                req.candidate = execution_candidate
            planner_pool = [
                c
                for c in self.cfg.candidates
                if c.role == "planner" and c.name != candidate.name
            ]
            planner_advice: list[str] = []
            for planner_index in range(
                min(intelligence_plan.planners, len(planner_pool))
            ):
                allocation_id = None
                try:
                    planner_decision = self.scheduler.choose(
                        job,
                        planner_pool,
                        role="planner",
                        attempt_no=attempt_no,
                        exclude={x.name for x in planner_pool[:planner_index]},
                    )
                    planner = planner_decision.candidate
                    allocation_id = self.db.begin_intelligence_allocation(
                        job.id,
                        attempt_id,
                        "PLANNER",
                        planner.name,
                        intelligence_plan.reason,
                        intelligence_plan.uncertainty,
                    )
                    accounting = ModelRequestAccounting(
                        self.db,
                        job_id=job.id,
                        attempt_id=attempt_id,
                        candidate=planner,
                        budget=job.budget,
                    )
                    advice = advisory_call(
                        planner,
                        accounting,
                        "Provide a concise implementation plan and identify hidden risks. "
                        "Do not write code.\n\n" + prompt,
                    )
                    planner_advice.append(advice)
                    self.db.finish_intelligence_allocation(
                        allocation_id, "SUCCEEDED", {"advice": advice}
                    )
                except Exception as exc:
                    if allocation_id is not None:
                        self.db.finish_intelligence_allocation(
                            allocation_id, "FAILED", {"error": str(exc)}
                        )
            if planner_advice:
                current_plan = [
                    line.strip(" -*\t")[:500]
                    for advice in planner_advice
                    for line in advice.splitlines()
                    if line.strip()
                ][:20]
                prompt += "\n\nINDEPENDENT PLANNING ADVICE:\n" + "\n\n---\n\n".join(
                    planner_advice
                )
                req.prompt = prompt
                augmented_manifest = dict(attempt_context_manifest)
                augmented_manifest["planner_advice_hash"] = stable_hash(planner_advice)
                self.db.update_attempt_context(
                    attempt_id,
                    stable_hash(
                        {
                            "base": attempt_context_hash,
                            "planner_advice": planner_advice,
                        }
                    ),
                    augmented_manifest,
                )
                self.reporter.text(
                    job.id, f"attempt-{attempt_no:02d}-prompt.txt", prompt + "\n"
                )
                self.reporter.json(
                    job.id,
                    f"attempt-{attempt_no:02d}-planning.json",
                    {"advice": planner_advice, "reason": intelligence_plan.reason},
                )
            if request_accounted_executor:
                req.accounting = ModelRequestAccounting(
                    self.db,
                    job_id=job.id,
                    attempt_id=attempt_id,
                    candidate=candidate,
                    budget=job.budget,
                )
                if candidate.executor == "bash" and self.cfg.research_enabled:
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
                if credential_reservation:
                    self.db.reconcile_provider_credential(
                        credential_reservation["reservation_id"],
                        status_code=None,
                        actual_tokens=0,
                        headers={},
                    )
                self.db.set_state(job.id, JobState.RETRY)
                total_attempts += 1
                cycle_attempts += 1
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
            if credential_reservation:
                provider_status = result.raw_metrics.get("provider_status")
                self.db.reconcile_provider_credential(
                    credential_reservation["reservation_id"],
                    status_code=(
                        int(provider_status)
                        if provider_status is not None
                        else (
                            401
                            if result.outcome == Outcome.PROVIDER_FAILURE
                            and "401" in (result.error or "")
                            else None
                        )
                    ),
                    actual_tokens=(result.input_tokens or 0)
                    + (result.output_tokens or 0),
                    headers=dict(result.raw_metrics.get("rate_headers") or {}),
                )
            if request_accounted_executor:
                result.accounting_level = "per_model_request"
            elif result.accounting_level == "unknown":
                result.accounting_level = {
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
            elif request_accounted_executor:
                totals = self.db.model_request_totals(attempt_id, candidate.name)
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
            cycle_attempts += 1
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
                self._capture_work_state(job, attempt_no, failures, current_plan)
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
                    if result.raw_metrics.get("context_incompatible"):
                        # Condensation already ran before the physical request.
                        # Preserve partial repository work and hand it to a
                        # candidate/provider whose
                        # sustainable per-request capacity fits this working set.
                        retry_exclude.add(candidate.name)
                    else:
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
            verifier_sandbox, verifier_config, verifier_source = (
                select_verifier_runtime(
                    repo_cfg,
                    self.cfg.toolchains,
                    candidate.extra,
                    self.cfg.verifier_sandbox,
                )
            )
            self.db.event(
                "VERIFIER_RESOURCE_SELECTED",
                job_id=job.id,
                attempt_id=attempt_id,
                payload={
                    "sandbox": verifier_sandbox,
                    "source": verifier_source,
                    "toolchain": repo_cfg.get("toolchain"),
                    "resource_class": verifier_config.get("resource_class"),
                    "remote_host": verifier_config.get("remote_host"),
                    "remote_instance": verifier_config.get("remote_instance"),
                    "network_policy": "none",
                },
            )
            v = verify(
                job,
                job.worktree,
                repo_cfg,
                verifier_sandbox,
                verifier_config,
            )
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
                self._capture_work_state(job, attempt_no, failures, current_plan)
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

            challenger_advice = ""
            challenger_pool = [
                c
                for c in self.cfg.candidates
                if c.role == "challenger" and c.name != candidate.name
            ]
            if intelligence_plan.challenger and challenger_pool:
                challenger_decision = self.scheduler.choose(
                    job, challenger_pool, role="challenger", attempt_no=attempt_no
                )
                challenger = challenger_decision.candidate
                allocation_id = self.db.begin_intelligence_allocation(
                    job.id,
                    attempt_id,
                    "CHALLENGER",
                    challenger.name,
                    intelligence_plan.reason,
                    intelligence_plan.uncertainty,
                )
                try:
                    challenger_accounting = ModelRequestAccounting(
                        self.db,
                        job_id=job.id,
                        attempt_id=attempt_id,
                        candidate=challenger,
                        budget=job.budget,
                    )
                    challenger_advice = advisory_call(
                        challenger,
                        challenger_accounting,
                        "Act as an independent challenger. Inspect this verified diff and identify "
                        "a materially better solution or serious hidden defect. Be concise; do not edit.\n\n"
                        + self.worktrees.diff(job.worktree, job.baseline_commit),
                    )
                    self.db.finish_intelligence_allocation(
                        allocation_id, "SUCCEEDED", {"opinion": challenger_advice}
                    )
                    self.reporter.json(
                        job.id,
                        f"attempt-{attempt_no:02d}-challenger.json",
                        {"candidate": challenger.name, "opinion": challenger_advice},
                    )
                except Exception as exc:
                    self.db.finish_intelligence_allocation(
                        allocation_id, "FAILED", {"error": str(exc)}
                    )

            automatic_review = intelligence_plan.reviewers > 0
            if (
                self.cfg.review_enabled or automatic_review
            ) and job.budget.max_reviews > 0:
                self.db.set_state(job.id, JobState.REVIEWING)
                reviewer_pool = [
                    c
                    for c in self.cfg.candidates
                    if c.role == "reviewer" and c.name != candidate.name
                ]
                if reviewer_pool:
                    rejected_review = None
                    reviewer_used: set[str] = set()
                    review_count = min(
                        intelligence_plan.reviewers or 1,
                        job.budget.max_reviews,
                        len(reviewer_pool),
                    )
                    for _ in range(review_count):
                        review_decision = self.scheduler.choose(
                            job,
                            reviewer_pool,
                            role="reviewer",
                            attempt_no=attempt_no,
                            exclude=reviewer_used,
                        )
                        reviewer_used.add(review_decision.candidate.name)
                        allocation_id = self.db.begin_intelligence_allocation(
                            job.id,
                            attempt_id,
                            "REVIEWER",
                            review_decision.candidate.name,
                            intelligence_plan.reason,
                            intelligence_plan.uncertainty,
                        )
                        review_accounting = ModelRequestAccounting(
                            self.db,
                            job_id=job.id,
                            attempt_id=attempt_id,
                            candidate=review_decision.candidate,
                            budget=job.budget,
                        )
                        review = ReviewerService().review(
                            job=job,
                            worktree=job.worktree,
                            candidate=review_decision.candidate,
                            attempt_no=attempt_no,
                            verification_summary=self._verification_summary(v)
                            + (
                                "\nCHALLENGER OPINION:\n" + challenger_advice
                                if challenger_advice
                                else ""
                            ),
                            accounting=review_accounting,
                        )
                        self.db.finish_intelligence_allocation(
                            allocation_id,
                            "SUCCEEDED" if review.ok else "REJECTED",
                            {
                                "verdict": review.verdict,
                                "summary": review.summary,
                                "findings": review.findings,
                            },
                        )
                        self.db.record_review(
                            attempt_id, review_decision.candidate.name, review
                        )
                        self.reporter.json(
                            job.id,
                            f"review-{attempt_no:02d}-{len(reviewer_used)}.json",
                            review,
                        )
                        if not review.ok:
                            rejected_review = review
                            break
                    if rejected_review:
                        self.db.finish_attempt(
                            attempt_id,
                            AttemptStatus.REVIEW_FAILED.value,
                            result,
                            duration,
                            outcome=Outcome.REVIEW_FAILURE.value,
                        )
                        failures.append(
                            f"Attempt {attempt_no} independent review rejected:\n"
                            + rejected_review.summary
                            + "\n"
                            + "\n".join(rejected_review.findings)
                        )
                        self._capture_work_state(
                            job, attempt_no, failures, current_plan
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

    def accept(
        self,
        job_id: str,
        message: str | None = None,
        *,
        review_seconds: float | None = None,
        human_changed_lines: int | None = None,
    ) -> str:
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
            allow_empty=bool(job.constraints.get("allow_no_changes", False)),
        )
        ready_attempt = self.db.latest_ready_attempt_id(job.id)
        if ready_attempt is None:
            raise RuntimeError("job has no READY attempt to accept")
        self.db.complete_acceptance(job.id, ready_attempt, commit)
        self.db.record_human_metrics(
            job.id,
            ready_attempt,
            review_seconds=review_seconds,
            changed_lines=human_changed_lines,
            accepted_without_edit=(human_changed_lines == 0)
            if human_changed_lines is not None
            else None,
        )
        job.state = JobState.ACCEPTED
        self.reporter.summary(job, commit=commit)
        self._write_event_audit(job.id)
        return commit

    def reject(
        self,
        job_id: str,
        feedback: str,
        *,
        review_seconds: float | None = None,
        repair_seconds: float | None = None,
        human_changed_lines: int | None = None,
    ) -> None:
        job = self.db.get_job(job_id)
        if job.state not in {
            JobState.READY,
            JobState.FAILED,
            JobState.RETRY,
            JobState.BLOCKED,
        }:
            raise RuntimeError(f"cannot reject job in state {job.state}")
        ready_attempt = self.db.latest_ready_attempt_id(job.id)
        self.db.add_feedback(
            job.id,
            "REJECT",
            feedback,
            attempt_id=ready_attempt,
        )
        if ready_attempt is not None:
            self.db.record_human_metrics(
                job.id,
                ready_attempt,
                review_seconds=review_seconds,
                repair_seconds=repair_seconds,
                changed_lines=human_changed_lines,
                accepted_without_edit=False,
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
