from pathlib import Path

from pmc.db import Database
from pmc.domain import Candidate, Job
from pmc.scheduler import Scheduler


def test_shared_resource_group_blocks_second_candidate(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    job = Job("PMC-000001", tmp_path, "task")
    db.create_job(job)
    a = Candidate(name="a", executor="bash", resource_group="potato", resource_concurrency=1)
    b = Candidate(name="b", executor="bash", resource_group="potato", resource_concurrency=1)
    db.begin_attempt(job.id, 1, a, "forced", 0.0)
    scheduler = Scheduler(db, 0.2, 1)
    availability = scheduler.available(b, [a, b])
    assert not availability.ok
    assert "potato" in availability.reason


def test_expired_lease_recovers_running_job(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    job = Job("PMC-000001", tmp_path, "task")
    db.create_job(job)
    c = Candidate(name="a", executor="bash")
    db.begin_attempt(job.id, 1, c, "forced", 0.0)
    db.set_state(job.id, __import__("pmc.domain", fromlist=["JobState"]).JobState.RUNNING)
    assert db.acquire_lease(job.id, "owner-a", 60)
    with db.connect() as conn:
        conn.execute("UPDATE leases SET expires_at=0 WHERE job_id=?", (job.id,))
    recovered = db.recover_expired_leases(max_attempts=3)
    assert recovered == [job.id]
    detail = db.job_detail(job.id)
    assert detail["job"]["state"] == "RETRY"
    assert detail["attempts"][0]["status"] == "EXECUTOR_FAILED"


def test_manual_baseline_stats(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    db.record_manual_baseline(
        request="fix", task_type="BUG_FIX", tool="codex", duration_seconds=12,
        accepted=True, cost_usd=0,
    )
    db.record_manual_baseline(
        request="fix2", task_type="BUG_FIX", tool="codex", duration_seconds=18,
        accepted=False, cost_usd=0,
    )
    row = db.manual_stats("BUG_FIX")[0]
    assert row["n"] == 2
    assert row["accepted"] == 1
    assert row["avg_seconds"] == 15
