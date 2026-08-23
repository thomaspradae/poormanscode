from __future__ import annotations

import subprocess
from pathlib import Path

from pmc.config import PMCConfig
from pmc.db import Database
from pmc.domain import Candidate, Job, JobState
from pmc.gitops import WorktreeManager
from pmc.scheduler import Scheduler
from pmc.verifier import verify


def sh(cwd: Path, command: str):
    return subprocess.run(
        ["bash", "-lc", command], cwd=cwd, check=True, text=True, capture_output=True
    )


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(repo, "git init -q -b main")
    sh(repo, "git config user.email test@example.com && git config user.name Test")
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_app.py").write_text(
        "from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    sh(repo, "git add -A && git commit -qm init")
    return repo


def test_worktree_and_verifier(tmp_path: Path):
    repo = make_repo(tmp_path)
    wm = WorktreeManager(tmp_path / "worktrees")
    wt, baseline = wm.create(repo, "PMC-000001", "main")
    (wt / "app.py").write_text("def add(a, b):\n    return a + b + 1\n")
    job = Job("PMC-000001", repo, "break it", baseline_commit=baseline, worktree=wt)
    v = verify(
        job, wt, {"test": "pytest -q", "max_patch_lines": 20, "max_files_changed": 2}
    )
    assert not v.ok
    assert v.changed_files == ["app.py"]
    assert any(c.name == "test" and not c.ok for c in v.commands)


def test_protected_and_secret_scan(tmp_path: Path):
    repo = make_repo(tmp_path)
    wm = WorktreeManager(tmp_path / "worktrees")
    wt, baseline = wm.create(repo, "PMC-000002", "main")
    (wt / ".env").write_text('API_KEY="sk-abcdefghijklmnopqrstuvwxyz123456"\n')
    job = Job("PMC-000002", repo, "oops", baseline_commit=baseline, worktree=wt)
    v = verify(
        job, wt, {"protected": [".env"], "max_patch_lines": 50, "max_files_changed": 5}
    )
    assert not v.ok
    assert not v.protected_paths_ok
    assert not v.secret_scan_ok


def test_scheduler_forces_cold_start_then_can_explore(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    job = Job("PMC-000001", tmp_path, "fix bug", task_type="BUG_FIX")
    db.create_job(job)
    candidates = [
        Candidate(name="a", executor="bash", model="m", base_url="x"),
        Candidate(name="b", executor="bash", model="m", base_url="x"),
    ]
    s = Scheduler(db, exploration_rate=0.2, min_samples=2)
    d = s.choose(job, candidates, attempt_no=1)
    assert d.mode == "cold_start"
    assert d.candidate.name in {"a", "b"}


def test_feedback_attaches_to_ready_attempt(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    job = Job("PMC-000001", tmp_path, "task")
    db.create_job(job)
    c = Candidate(name="a", executor="bash")
    aid = db.begin_attempt(job.id, 1, c, "forced", 0)
    from pmc.domain import ExecutionResult

    db.finish_attempt(aid, "READY", ExecutionResult(True), 1.0)
    assert db.latest_ready_attempt_id(job.id) == aid
    db.add_feedback(job.id, "ACCEPT", attempt_id=aid)
    stats = db.candidate_stats()
    assert stats[0]["accepted_attempts"] == 1


def test_verification_policy_is_loadable_from_immutable_baseline(tmp_path: Path):
    from pmc.config import load_repo_config_at

    repo = make_repo(tmp_path)
    (repo / "poorman.yaml").write_text("test: pytest -q\nmax_patch_lines: 10\n")
    sh(repo, "git add poorman.yaml && git commit -qm policy")
    baseline = sh(repo, "git rev-parse HEAD").stdout.strip()
    (repo / "poorman.yaml").write_text("test: 'true'\nmax_patch_lines: 99999\n")
    policy = load_repo_config_at(repo, baseline)
    assert policy == {"test": "pytest -q", "max_patch_lines": 10}
