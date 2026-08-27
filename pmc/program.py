from __future__ import annotations

import socket
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from .config import PMCConfig
from .controller import Controller
from .domain import JobState

PROGRAM_MODE = "AUTONOMOUS_PROGRAM"
VERIFIED_FOR_CHAINING = "VERIFIED_FOR_CHAINING"


@dataclass(frozen=True, slots=True)
class ProgramRunResult:
    feature_id: str
    state: str
    completed_jobs: int
    promoted_jobs: int


class ProgramRunner:
    """Run an approved DAG while keeping final acceptance human-only."""

    def __init__(
        self,
        cfg: PMCConfig,
        feature_id: str,
        *,
        workers: int | None = None,
        poll_seconds: float = 2.0,
        lease_seconds: int = 90,
    ) -> None:
        self.cfg = cfg
        self.feature_id = feature_id
        self.controller = Controller(cfg)
        feature = self.controller.db.get_feature(feature_id)
        if feature["mode"] != PROGRAM_MODE:
            raise RuntimeError(f"{feature_id} is not an autonomous program")
        configured = int(feature["max_workers"] or 1)
        self.workers = workers if workers is not None else configured
        if not 1 <= self.workers <= 16:
            raise ValueError("program workers must be between 1 and 16")
        if self.workers > configured:
            raise ValueError(
                f"requested {self.workers} workers exceeds approved ceiling {configured}"
            )
        self.poll_seconds = max(0.05, poll_seconds)
        self.lease_seconds = max(30, lease_seconds)
        self.owner = f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"

    def _run_job(self, job_id: str) -> JobState:
        # A controller per worker avoids sharing mutable provider/executor state.
        return Controller(self.cfg).run_job(job_id)

    def _promote_ready(self, *, exclude: set[str] | None = None) -> int:
        promoted = 0
        exclude = exclude or set()
        for row in self.controller.db.feature_tasks(self.feature_id):
            if row["job_id"] in exclude:
                continue
            if row["is_terminal"] or row["state"] != JobState.READY.value:
                continue
            if row["promotion_state"] == VERIFIED_FOR_CHAINING:
                continue
            job = self.controller.db.get_job(row["job_id"])
            if not job.worktree or not job.baseline_commit:
                raise RuntimeError(f"ready program task {job.id} has no preserved worktree")
            commit = self.controller.worktrees.commit_idempotent(
                job.worktree,
                job.baseline_commit,
                f"{job.id}: verified program checkpoint",
                job.id,
            )
            if self.controller.db.promote_program_task(job.id, commit):
                promoted += 1
        return promoted

    def _terminal_result(self) -> str | None:
        terminal_id = self.controller.db.program_terminal_job(self.feature_id)
        state = self.controller.db.get_job(terminal_id).state
        if state == JobState.READY:
            return "READY_FOR_REVIEW"
        if state == JobState.ACCEPTED:
            return "ACCEPTED"
        return None

    def run(self) -> ProgramRunResult:
        if not self.controller.db.claim_program_run(
            self.feature_id, self.owner, self.lease_seconds
        ):
            raise RuntimeError(f"{self.feature_id} is not runnable or is leased elsewhere")
        self.controller.db.event(
            "PROGRAM_RUN_STARTED",
            payload={
                "feature_id": self.feature_id,
                "owner": self.owner,
                "workers": self.workers,
            },
        )
        active: dict[Future[JobState], str] = {}
        completed = 0
        promoted = 0
        final_state = "BLOCKED"
        final_error: str | None = None
        try:
            with ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix="pmc-program"
            ) as pool:
                while True:
                    self.controller.db.recover_expired_leases(self.cfg.max_attempts)
                    feature = self.controller.db.get_feature(self.feature_id)
                    if feature["state"] in {"PAUSED", "CANCELLED"}:
                        final_state = str(feature["state"])
                        break
                    if not self.controller.db.heartbeat_program_run(
                        self.feature_id, self.owner, self.lease_seconds
                    ):
                        raise RuntimeError("program lease was lost")

                    # Crash-safe boundary: a previous process may have stopped after
                    # verification but before recording the internal commit.
                    promoted += self._promote_ready(exclude=set(active.values()))
                    terminal = self._terminal_result()
                    terminal_id = self.controller.db.program_terminal_job(
                        self.feature_id
                    )
                    if terminal and terminal_id not in active.values():
                        final_state = terminal
                        break

                    capacity = self.workers - len(active)
                    if capacity > 0 and not final_error:
                        running_ids = set(active.values())
                        ready = [
                            job_id
                            for job_id in self.controller.db.program_ready_jobs(
                                self.feature_id, capacity
                            )
                            if job_id not in running_ids
                        ]
                        for job_id in ready:
                            active[pool.submit(self._run_job, job_id)] = job_id

                    if active:
                        done, _ = wait(
                            active,
                            timeout=min(self.poll_seconds, self.lease_seconds / 3),
                            return_when=FIRST_COMPLETED,
                        )
                        for future in done:
                            job_id = active.pop(future)
                            completed += 1
                            try:
                                state = future.result()
                            except Exception as exc:  # noqa: BLE001 - persist worker crashes
                                self.controller.db.set_state(job_id, JobState.BLOCKED)
                                final_error = f"{job_id}: {type(exc).__name__}: {exc}"
                                state = JobState.BLOCKED
                            if state in {
                                JobState.BLOCKED,
                                JobState.FAILED,
                                JobState.CANCELLED,
                            }:
                                final_error = final_error or f"{job_id}: {state.value}"
                        if final_error:
                            # Let already-running siblings finish so their verified work
                            # remains recoverable, but dispatch no additional work.
                            if active:
                                continue
                            final_state = "BLOCKED"
                            break
                        continue

                    tasks = self.controller.db.feature_tasks(self.feature_id)
                    blocked = [
                        row
                        for row in tasks
                        if row["state"]
                        in {JobState.BLOCKED.value, JobState.FAILED.value, JobState.CANCELLED.value}
                    ]
                    if blocked:
                        final_error = ", ".join(
                            f"{row['job_id']}={row['state']}" for row in blocked
                        )
                        final_state = "BLOCKED"
                        break
                    if self.controller.db.program_inflight_jobs(self.feature_id):
                        # A restarted supervisor may observe jobs still owned by the
                        # previous controller. Wait for their job leases to expire;
                        # recover_expired_leases above will then make them retryable.
                        time.sleep(self.poll_seconds)
                        continue
                    # No active or dispatchable work means the dependency graph cannot
                    # advance. Treat it as a diagnosable deadlock, never an endless loop.
                    final_error = "no active or dependency-ready jobs; program cannot advance"
                    final_state = "BLOCKED"
                    break
        except Exception as exc:
            final_error = f"{type(exc).__name__}: {exc}"
            final_state = "BLOCKED"
            raise
        finally:
            self.controller.db.release_program_run(
                self.feature_id,
                self.owner,
                state=final_state,
                error=final_error,
            )
            self.controller.db.event(
                "PROGRAM_RUN_FINISHED",
                payload={
                    "feature_id": self.feature_id,
                    "state": final_state,
                    "completed_jobs": completed,
                    "promoted_jobs": promoted,
                    "error": final_error,
                },
            )
        return ProgramRunResult(self.feature_id, final_state, completed, promoted)
