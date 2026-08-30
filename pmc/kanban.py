"""A deliberately boring, single-writer project workflow.

Kanban mode is intentionally separate from PMC's branch-per-job program runner.
It exists for exploratory projects where a pushed checkpoint is more valuable
than a perfect final verifier result.  The database is shared, but Kanban owns
its own tables and never cherry-picks task branches.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .db import Database, utcnow
from .gitops import GitError, ensure_repo, git, resolve_commit


KANBAN_STATES = {
    "BACKLOG",
    "READY",
    "WORKING",
    "CHECKPOINTED",
    "NEEDS_REPAIR",
    "BLOCKED",
    "DONE",
}
VERIFICATION_MODES = {"SMOKE", "STANDARD", "STRICT"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS kanban_projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    verification_mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kanban_tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES kanban_projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    request TEXT NOT NULL,
    acceptance_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    executor TEXT,
    last_activity_at TEXT NOT NULL,
    last_checkpoint_commit TEXT,
    smoke_status TEXT,
    blocked_reason TEXT,
    work_state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kanban_tasks_project_state
ON kanban_tasks(project_id,state,created_at);
"""


@dataclass(frozen=True)
class SmokeResult:
    status: str  # PASS | WARNING | FATAL
    detail: str


def _safe(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-") or "project"


def _run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return git(repo, *args, check=check)


class Kanban:
    def __init__(self, db: Database, worktrees_root: Path):
        self.db = db
        self.root = worktrees_root / "projects"
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db.connect() as conn:
            conn.executescript(SCHEMA)

    def create_project(
        self,
        project_id: str,
        title: str,
        repo: Path,
        *,
        branch: str | None = None,
        verification_mode: str = "SMOKE",
    ) -> dict:
        if verification_mode not in VERIFICATION_MODES:
            raise ValueError(f"unknown verification mode: {verification_mode}")
        ensure_repo(repo)
        project_id = _safe(project_id)
        branch = branch or f"work/{project_id}"
        worktree = self.root / project_id
        now = utcnow()
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM kanban_projects WHERE id=?", (project_id,)
            ).fetchone()
            if existing:
                return dict(existing)
            conn.execute(
                """INSERT INTO kanban_projects(id,title,repo,branch,worktree,verification_mode,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (project_id, title, str(repo.resolve()), branch, str(worktree), verification_mode, now, now),
            )
        try:
            self.ensure_worktree(project_id)
        except Exception:
            # Do not leave a project that looks usable if its persistent worktree
            # could not be made/adopted.
            with self.db.connect() as conn:
                conn.execute("DELETE FROM kanban_projects WHERE id=?", (project_id,))
            raise
        return self.project(project_id)

    def project(self, project_id: str) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM kanban_projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown Kanban project: {project_id}")
        return dict(row)

    def ensure_worktree(self, project_id: str) -> Path:
        project = self.project(project_id)
        repo = Path(project["repo"])
        worktree = Path(project["worktree"])
        branch = project["branch"]
        ensure_repo(repo)
        if worktree.exists():
            if _run(worktree, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
                raise GitError(f"Kanban worktree is not a Git worktree: {worktree}")
            actual = _run(worktree, "branch", "--show-current").stdout.strip()
            if actual != branch:
                raise GitError(f"Kanban worktree branch is {actual!r}, expected {branch!r}")
            return worktree
        _run(repo, "fetch", "--prune", check=False)  # Offline work remains valid.
        branch_exists = _run(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0
        if branch_exists:
            command = ["worktree", "add", str(worktree), branch]
        else:
            base = resolve_commit(repo, "HEAD")
            command = ["worktree", "add", "-b", branch, str(worktree), base]
        _run(repo, *command)
        return worktree

    def add_task(self, project_id: str, task_id: str, title: str, request: str, acceptance: list[str]) -> dict:
        self.project(project_id)
        task_id = _safe(task_id)
        now = utcnow()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO kanban_tasks(id,project_id,title,request,acceptance_json,state,last_activity_at,created_at,updated_at)
                VALUES(?,?,?,?,?,'READY',?,?,?)""",
                (task_id, project_id, title, request, json.dumps(acceptance), now, now, now),
            )
        return self.task(task_id)

    def task(self, task_id: str) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM kanban_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown Kanban task: {task_id}")
        return dict(row)

    def _active_task(self, project_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM kanban_tasks WHERE project_id=? AND state='WORKING' ORDER BY updated_at LIMIT 1",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def start(self, task_id: str, executor: str) -> dict:
        task = self.task(task_id)
        active = self._active_task(task["project_id"])
        if active and active["id"] != task_id:
            raise RuntimeError(f"single-writer project is busy with {active['id']}")
        if task["state"] not in {"READY", "CHECKPOINTED", "NEEDS_REPAIR", "WORKING"}:
            raise RuntimeError(f"cannot start task in state {task['state']}")
        self.ensure_worktree(task["project_id"])
        now = utcnow()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE kanban_tasks SET state='WORKING',executor=?,last_activity_at=?,updated_at=?,blocked_reason=NULL WHERE id=?",
                (executor, now, now, task_id),
            )
        self.capture_work_state(task_id)
        return self.task(task_id)

    def capture_work_state(self, task_id: str, *, failure: str | None = None) -> dict:
        task = self.task(task_id)
        project = self.project(task["project_id"])
        worktree = self.ensure_worktree(task["project_id"])
        status = _run(worktree, "status", "--short").stdout.splitlines()
        diff = _run(worktree, "diff", "--stat").stdout.strip()
        state = {
            "task": task["request"],
            "acceptance": json.loads(task["acceptance_json"]),
            "repository_commit": resolve_commit(worktree, "HEAD"),
            "branch": project["branch"],
            "changed_files": [line[3:] for line in status if len(line) > 3],
            "git_status": status,
            "diff_stat": diff,
            "latest_failure": failure,
            "captured_at": utcnow(),
        }
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE kanban_tasks SET work_state_json=?,last_activity_at=?,updated_at=? WHERE id=?",
                (json.dumps(state), utcnow(), utcnow(), task_id),
            )
        return state

    def checkpoint(self, task_id: str, message: str) -> str:
        task = self.task(task_id)
        if task["state"] != "WORKING":
            raise RuntimeError("only a WORKING task can checkpoint")
        worktree = self.ensure_worktree(task["project_id"])
        _run(worktree, "add", "-A")
        if _run(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
            raise RuntimeError("no changes to checkpoint")
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "Poor Man's Code")
        env.setdefault("GIT_AUTHOR_EMAIL", "pmc@localhost")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        proc = subprocess.run(["git", "-C", str(worktree), "commit", "-m", f"{task_id} checkpoint: {message}"], text=True, capture_output=True, env=env)
        if proc.returncode:
            raise GitError(proc.stderr.strip())
        commit = resolve_commit(worktree, "HEAD")
        project = self.project(task["project_id"])
        push = _run(worktree, "push", "origin", f"HEAD:{project['branch']}", check=False)
        if push.returncode:
            text = (push.stderr + "\n" + push.stdout).lower()
            reason = "REMOTE_DIVERGED" if any(x in text for x in ("non-fast-forward", "fetch first", "rejected")) else "GIT_PUSH_FAILED"
            self.block(task_id, f"{reason}: {(push.stderr or push.stdout).strip()}")
            raise GitError(f"checkpoint {commit} exists locally but push failed: {reason}")
        now = utcnow()
        with self.db.connect() as conn:
            conn.execute("UPDATE kanban_tasks SET state='CHECKPOINTED',last_checkpoint_commit=?,last_activity_at=?,updated_at=? WHERE id=?", (commit, now, now, task_id))
        self.capture_work_state(task_id)
        return commit

    def smoke(self, task_id: str, command: str) -> SmokeResult:
        task = self.task(task_id)
        worktree = self.ensure_worktree(task["project_id"])
        proc = subprocess.run(command, shell=True, cwd=worktree, text=True, capture_output=True)
        detail = (proc.stdout + "\n" + proc.stderr).strip()[-4000:]
        result = SmokeResult("PASS" if proc.returncode == 0 else "FATAL", detail)
        now = utcnow()
        next_state = "CHECKPOINTED" if result.status == "PASS" and task["last_checkpoint_commit"] else ("NEEDS_REPAIR" if result.status == "FATAL" else task["state"])
        with self.db.connect() as conn:
            conn.execute("UPDATE kanban_tasks SET state=?,smoke_status=?,last_activity_at=?,updated_at=? WHERE id=?", (next_state, result.status, now, now, task_id))
        self.capture_work_state(task_id, failure=detail if result.status == "FATAL" else None)
        return result

    def warning(self, task_id: str, detail: str) -> None:
        """Record nonfatal smoke evidence without rejecting useful work."""
        now = utcnow()
        with self.db.connect() as conn:
            conn.execute("UPDATE kanban_tasks SET smoke_status='WARNING',last_activity_at=?,updated_at=? WHERE id=?", (now, now, task_id))

    def block(self, task_id: str, reason: str) -> None:
        now = utcnow()
        with self.db.connect() as conn:
            conn.execute("UPDATE kanban_tasks SET state='BLOCKED',blocked_reason=?,last_activity_at=?,updated_at=? WHERE id=?", (reason[:8000], now, now, task_id))
        self.capture_work_state(task_id, failure=reason)

    def finish(self, task_id: str) -> None:
        task = self.task(task_id)
        if task["smoke_status"] not in {"PASS", "WARNING"}:
            raise RuntimeError("a task needs smoke evidence before DONE")
        now = utcnow()
        with self.db.connect() as conn:
            conn.execute("UPDATE kanban_tasks SET state='DONE',last_activity_at=?,updated_at=? WHERE id=?", (now, now, task_id))

    def next_ready(self, project_id: str) -> dict | None:
        if self._active_task(project_id):
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM kanban_tasks WHERE project_id=? AND state IN ('READY','CHECKPOINTED','NEEDS_REPAIR') ORDER BY CASE state WHEN 'READY' THEN 0 WHEN 'NEEDS_REPAIR' THEN 1 ELSE 2 END, created_at LIMIT 1", (project_id,)).fetchone()
        return dict(row) if row else None

    def diagnose_working(self, project_id: str, alive_task_ids: set[str]) -> list[str]:
        """Release phantom WORKING tasks; diagnosis never retries an executor.

        A real watchdog supplies the task IDs it can prove are executing or in
        a valid provider wait.  Everything else becomes NEEDS_REPAIR, leaving
        the writer slot available for an explicit resume or another task.
        """
        stale: list[str] = []
        for task in self.board(project_id):
            if task["state"] == "WORKING" and task["id"] not in alive_task_ids:
                stale.append(task["id"])
                now = utcnow()
                with self.db.connect() as conn:
                    conn.execute(
                        "UPDATE kanban_tasks SET state='NEEDS_REPAIR',blocked_reason=?,last_activity_at=?,updated_at=? WHERE id=?",
                        ("WATCHDOG_NO_ACTIVE_EXECUTION", now, now, task["id"]),
                    )
                self.capture_work_state(task["id"], failure="WATCHDOG_NO_ACTIVE_EXECUTION")
        return stale

    def board(self, project_id: str) -> list[dict]:
        self.project(project_id)
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM kanban_tasks WHERE project_id=? ORDER BY CASE state WHEN 'WORKING' THEN 0 WHEN 'NEEDS_REPAIR' THEN 1 WHEN 'READY' THEN 2 WHEN 'CHECKPOINTED' THEN 3 WHEN 'BLOCKED' THEN 4 WHEN 'DONE' THEN 5 ELSE 6 END, created_at", (project_id,)).fetchall()
        return [dict(row) for row in rows]
