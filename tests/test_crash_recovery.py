import multiprocessing
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from pmc.accounting import ModelRequestAccounting
from pmc.db import Database
from pmc.domain import Candidate, Job, JobState, SchedulerDecision
from pmc.gitops import WorktreeManager, resolve_commit


def _crash_during_transition(db_path: str, phase: str) -> None:
    db = Database(Path(db_path))
    job_id = "PMC-000001"
    db.acquire_lease(job_id, "dead-controller", 0.01)
    candidate = Candidate(name="a", executor="bash", model="m", provider="p")
    db.record_decision(
        job_id,
        1,
        SchedulerDecision(candidate, "explore", 0.5, "durable", ["a"], {}, 1.0),
    )
    if phase != "dispatch":
        attempt = db.begin_attempt(job_id, 1, candidate, "forced", 0)
        db.reserve_resource("machine", job_id, attempt, "dead-controller", 60, {})
        if phase == "model_request":
            accounting = ModelRequestAccounting(
                db, job_id=job_id, attempt_id=attempt, candidate=candidate
            )
            accounting.reserve(1, [{"role": "user", "content": "task"}], 100)
        if phase == "ready":
            from pmc.domain import ExecutionResult

            db.finish_attempt(
                attempt, "READY", ExecutionResult(True), 1, outcome="SUCCESS"
            )
            db.release_resource("machine", job_id)
            db.set_state(job_id, JobState.READY)
        elif phase == "verification":
            db.set_state(job_id, JobState.VERIFYING)
        elif phase == "review":
            db.set_state(job_id, JobState.REVIEWING)
        else:
            db.set_state(job_id, JobState.RUNNING)
    else:
        db.set_state(job_id, JobState.RUNNING)
    os.kill(os.getpid(), signal.SIGKILL)


@pytest.mark.parametrize(
    "phase", ["dispatch", "model_request", "execution", "verification", "review"]
)
def test_sigkill_recovery_has_no_duplicate_attempt_or_leak(tmp_path: Path, phase: str):
    db = Database(tmp_path / "pmc.db")
    db.create_job(Job("PMC-000001", tmp_path, "task"))
    process = multiprocessing.get_context("fork").Process(
        target=_crash_during_transition, args=(str(db.path), phase)
    )
    process.start()
    process.join(5)
    assert process.exitcode == -signal.SIGKILL
    time.sleep(0.03)
    assert db.recover_expired_leases(3) == ["PMC-000001"]
    assert db.get_job("PMC-000001").state == JobState.RETRY
    assert db.attempt_count("PMC-000001") == (0 if phase == "dispatch" else 1)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM resource_leases").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM quota_reservations WHERE state='RESERVED'"
            ).fetchone()[0]
            == 0
        )
        if phase == "model_request":
            assert (
                conn.execute("SELECT state FROM model_requests").fetchone()[0]
                == "FAILED"
            )


def test_sigkill_after_ready_state_preserves_ready_and_cleans_lease(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    db.create_job(Job("PMC-000001", tmp_path, "task"))
    process = multiprocessing.get_context("fork").Process(
        target=_crash_during_transition, args=(str(db.path), "ready")
    )
    process.start()
    process.join(5)
    assert process.exitcode == -signal.SIGKILL
    time.sleep(0.03)
    assert db.recover_expired_leases(3) == []
    assert db.get_job("PMC-000001").state == JobState.READY
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0


def _commit_then_crash(repo: str, baseline: str) -> None:
    WorktreeManager(Path(repo).parent / "worktrees").commit_idempotent(
        Path(repo), baseline, "accept", "PMC-000001"
    )
    os.kill(os.getpid(), signal.SIGKILL)


def test_sigkill_after_commit_does_not_duplicate_commit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        "git init -q -b main && git config user.name Test && git config user.email t@example.com",
        cwd=repo,
        shell=True,
        check=True,
    )
    (repo / "a").write_text("one")
    subprocess.run(
        "git add a && git commit -qm baseline", cwd=repo, shell=True, check=True
    )
    baseline = resolve_commit(repo, "HEAD")
    (repo / "a").write_text("two")
    process = multiprocessing.get_context("fork").Process(
        target=_commit_then_crash, args=(str(repo), baseline)
    )
    process.start()
    process.join(5)
    assert process.exitcode == -signal.SIGKILL
    manager = WorktreeManager(tmp_path / "worktrees")
    recovered = manager.commit_idempotent(repo, baseline, "accept", "PMC-000001")
    assert recovered == resolve_commit(repo, "HEAD")
    count = subprocess.run(
        "git rev-list --count HEAD",
        cwd=repo,
        shell=True,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert count == "2"
