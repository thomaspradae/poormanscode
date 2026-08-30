from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pmc.db import Database
from pmc.gitops import GitError
from pmc.kanban import Kanban


def sh(cwd: Path, command: str) -> str:
    return subprocess.run(
        ["bash", "-lc", command], cwd=cwd, text=True, check=True, capture_output=True
    ).stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    sh(tmp_path, f"git init -q --bare {remote}")
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(repo, "git init -q -b main && git config user.email test@example.com && git config user.name Test")
    (repo / "readme.md").write_text("baseline\n")
    sh(repo, "git add -A && git commit -qm baseline")
    sh(repo, f"git remote add origin {remote} && git push -qu origin main")
    return repo, remote


def setup_board(tmp_path: Path) -> tuple[Kanban, Path]:
    repo, _ = make_repo(tmp_path)
    board = Kanban(Database(tmp_path / "pmc.db"), tmp_path / "worktrees")
    board.create_project("football", "Football World Lab", repo)
    return board, repo


def add(board: Kanban, task_id: str = "people") -> None:
    board.add_task("football", task_id, task_id, f"implement {task_id}", ["smoke runs"])


def test_checkpoint_survives_executor_failure_and_can_resume(tmp_path: Path):
    board, _ = setup_board(tmp_path)
    add(board)
    board.start("people", "jules")
    worktree = Path(board.project("football")["worktree"])
    (worktree / "people.txt").write_text("seeded people\n")
    commit = board.checkpoint("people", "add deterministic people seed")
    assert board.task("people")["state"] == "CHECKPOINTED"
    assert board.task("people")["last_checkpoint_commit"] == commit
    board.start("people", "jules-2")
    state = board.task("people")
    assert state["state"] == "WORKING"
    assert commit == state["last_checkpoint_commit"]
    assert (worktree / "people.txt").read_text() == "seeded people\n"


def test_blocked_task_releases_slot_for_an_independent_ready_task(tmp_path: Path):
    board, _ = setup_board(tmp_path)
    add(board, "people")
    add(board, "relationships")
    board.start("people", "jules")
    board.block("people", "REMOTE_DIVERGED")
    assert board.next_ready("football")["id"] == "relationships"
    board.start("relationships", "jules")
    assert board.task("relationships")["state"] == "WORKING"


def test_smoke_failure_is_repair_not_global_rejection_and_warning_is_nonfatal(tmp_path: Path):
    board, _ = setup_board(tmp_path)
    add(board)
    board.start("people", "jules")
    failed = board.smoke("people", "false")
    assert failed.status == "FATAL"
    assert board.task("people")["state"] == "NEEDS_REPAIR"
    board.warning("people", "missing exhaustive tests")
    assert board.task("people")["smoke_status"] == "WARNING"


def test_deterministic_push_failure_blocks_once_without_retrying(tmp_path: Path, monkeypatch):
    board, _ = setup_board(tmp_path)
    add(board)
    board.start("people", "jules")
    worktree = Path(board.project("football")["worktree"])
    (worktree / "people.txt").write_text("x\n")
    calls = 0
    import pmc.kanban as module

    original = module._run

    def reject_push(repo: Path, *args: str, **kwargs):
        nonlocal calls
        if args and args[0] == "push":
            calls += 1
            return subprocess.CompletedProcess(["git"], 1, "", "! [rejected] non-fast-forward")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(module, "_run", reject_push)
    with pytest.raises(GitError):
        board.checkpoint("people", "will block")
    assert calls == 1
    assert board.task("people")["state"] == "BLOCKED"
    assert "REMOTE_DIVERGED" in board.task("people")["blocked_reason"]


def test_watchdog_diagnoses_phantom_working_without_retry(tmp_path: Path):
    board, _ = setup_board(tmp_path)
    add(board)
    board.start("people", "jules")
    assert board.diagnose_working("football", set()) == ["people"]
    assert board.task("people")["state"] == "NEEDS_REPAIR"
    assert board.task("people")["blocked_reason"] == "WATCHDOG_NO_ACTIVE_EXECUTION"
