from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from pmc.config import PMCConfig
from pmc.controller import Controller
from pmc.domain import Job, JobState
from pmc.foreman import validate_plan
from pmc.program import ProgramRunner


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _program(tmp_path: Path) -> tuple[PMCConfig, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Program Test")
    _git(repo, "config", "user.email", "program@example.invalid")
    (repo / "README.md").write_text("program\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "baseline")
    base_commit = _git(repo, "rev-parse", "HEAD")
    cfg = PMCConfig(
        db_path=tmp_path / "pmc.db",
        runs_dir=tmp_path / "runs",
        worktrees_dir=tmp_path / "worktrees",
        candidates=[],
    )
    ctl = Controller(cfg)
    feature_id = ctl.db.next_feature_id()
    ctl.db.create_feature(
        feature_id,
        repo,
        "Autonomous program",
        "Build in verified stages",
        "main",
        base_commit=base_commit,
        mode="AUTONOMOUS_PROGRAM",
        max_workers=3,
    )
    plan = validate_plan(
        {
            "tasks": [
                {"id": "foundation", "request": "foundation", "depends_on": []},
                {
                    "id": "left",
                    "request": "left",
                    "depends_on": ["foundation"],
                },
                {
                    "id": "right",
                    "request": "right",
                    "depends_on": ["foundation"],
                },
                {
                    "id": "integration",
                    "request": "integration",
                    "depends_on": ["left", "right"],
                },
            ]
        }
    )
    ctl.db.save_feature_plan(feature_id, plan, None)
    jobs = []
    for position, task in enumerate(plan["tasks"]):
        jobs.append(
            (
                Job(
                    f"PMC-{position + 1:06d}",
                    repo,
                    task["request"],
                    base_branch=base_commit,
                    constraints={"_feature_dependencies": task["depends_on"]},
                ),
                task["id"],
                position,
                task["depends_on"],
            )
        )
    ctl.db.approve_feature_plan(feature_id, jobs)
    return cfg, feature_id


def test_program_runs_parallel_branches_and_stops_for_final_human_review(
    tmp_path: Path, monkeypatch
):
    cfg, feature_id = _program(tmp_path)
    lock = threading.Lock()
    active = 0
    maximum = 0

    def fake_run_job(self: Controller, job_id: str, forced_candidate=None):
        nonlocal active, maximum
        job = self.ensure_worktree(self.db.get_job(job_id))
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            if job_id in {"PMC-000002", "PMC-000003"}:
                time.sleep(0.08)
            assert job.worktree is not None
            (job.worktree / f"{job_id}.txt").write_text(f"{job_id}\n")
            self.db.set_state(job_id, JobState.READY)
            return JobState.READY
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(Controller, "run_job", fake_run_job)
    result = ProgramRunner(cfg, feature_id, workers=3, poll_seconds=0.01).run()

    assert result.state == "READY_FOR_REVIEW"
    assert result.completed_jobs == 4
    assert result.promoted_jobs == 3
    assert maximum >= 2
    ctl = Controller(cfg)
    rows = ctl.db.feature_tasks(feature_id)
    for row in rows[:-1]:
        assert row["state"] == JobState.READY.value
        assert row["promotion_state"] == "VERIFIED_FOR_CHAINING"
        assert row["verified_commit"]
        assert row["accepted_commit"] is None
    terminal = rows[-1]
    assert terminal["is_terminal"] == 1
    assert terminal["state"] == JobState.READY.value
    assert terminal["promotion_state"] == "WAITING"
    terminal_job = ctl.db.get_job(terminal["job_id"])
    assert terminal_job.worktree is not None
    assert (terminal_job.worktree / "PMC-000001.txt").exists()
    assert (terminal_job.worktree / "PMC-000002.txt").exists()
    assert (terminal_job.worktree / "PMC-000003.txt").exists()
    assert (terminal_job.worktree / "PMC-000004.txt").exists()


def test_program_crash_boundary_promotes_existing_ready_work(tmp_path: Path, monkeypatch):
    cfg, feature_id = _program(tmp_path)
    ctl = Controller(cfg)
    job = ctl.ensure_worktree(ctl.db.get_job("PMC-000001"))
    assert job.worktree is not None
    (job.worktree / "recovered.txt").write_text("verified before crash\n")
    ctl.db.set_state(job.id, JobState.READY)

    original = Controller.run_job

    def fake_run_job(self: Controller, job_id: str, forced_candidate=None):
        if job_id == "PMC-000001":
            raise AssertionError("ready work must be promoted, not rerun")
        job = self.ensure_worktree(self.db.get_job(job_id))
        assert job.worktree is not None
        (job.worktree / f"{job_id}.txt").write_text("done\n")
        self.db.set_state(job_id, JobState.READY)
        return JobState.READY

    monkeypatch.setattr(Controller, "run_job", fake_run_job)
    result = ProgramRunner(cfg, feature_id, workers=2, poll_seconds=0.01).run()
    assert result.state == "READY_FOR_REVIEW"
    assert Controller(cfg).db.feature_tasks(feature_id)[0]["verified_commit"]
    monkeypatch.setattr(Controller, "run_job", original)


def test_program_stops_dispatch_after_a_blocked_branch(tmp_path: Path, monkeypatch):
    cfg, feature_id = _program(tmp_path)
    executed: list[str] = []

    def fake_run_job(self: Controller, job_id: str, forced_candidate=None):
        executed.append(job_id)
        job = self.ensure_worktree(self.db.get_job(job_id))
        assert job.worktree is not None
        (job.worktree / f"{job_id}.txt").write_text("work\n")
        state = JobState.BLOCKED if job_id == "PMC-000002" else JobState.READY
        self.db.set_state(job_id, state)
        return state

    monkeypatch.setattr(Controller, "run_job", fake_run_job)
    result = ProgramRunner(cfg, feature_id, workers=2, poll_seconds=0.01).run()
    assert result.state == "BLOCKED"
    assert "PMC-000004" not in executed
    assert Controller(cfg).db.get_feature(feature_id)["last_error"]


def test_program_worker_ceiling_and_single_supervisor_lease(tmp_path: Path):
    cfg, feature_id = _program(tmp_path)
    try:
        ProgramRunner(cfg, feature_id, workers=4)
    except ValueError as exc:
        assert "approved ceiling 3" in str(exc)
    else:
        raise AssertionError("worker ceiling was not enforced")

    db = Controller(cfg).db
    assert db.claim_program_run(feature_id, "owner-a", 60)
    assert not db.claim_program_run(feature_id, "owner-b", 60)
    db.release_program_run(feature_id, "owner-a", state="APPROVED")
    assert db.claim_program_run(feature_id, "owner-b", 60)
