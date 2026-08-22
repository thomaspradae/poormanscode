from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .domain import Job, JobState, VerificationResult


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    request TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    priority INTEGER NOT NULL,
    task_type TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    state TEXT NOT NULL,
    worktree TEXT,
    baseline_commit TEXT,
    accepted_commit TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    attempt_no INTEGER NOT NULL,
    candidate TEXT NOT NULL,
    executor TEXT NOT NULL,
    model TEXT,
    role TEXT NOT NULL,
    selection_mode TEXT NOT NULL,
    selection_score REAL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_seconds REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    provider_request_id TEXT,
    summary TEXT,
    error TEXT,
    raw_metrics_json TEXT,
    UNIQUE(job_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    ok INTEGER NOT NULL,
    patch_lines INTEGER NOT NULL,
    changed_files_json TEXT NOT NULL,
    scope_ok INTEGER NOT NULL,
    secret_scan_ok INTEGER NOT NULL,
    protected_paths_ok INTEGER NOT NULL,
    dependencies_ok INTEGER NOT NULL,
    findings_json TEXT NOT NULL,
    commands_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    reviewer_candidate TEXT,
    verdict TEXT NOT NULL,
    ok INTEGER NOT NULL,
    summary TEXT,
    findings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    attempt_id INTEGER REFERENCES attempts(id),
    verdict TEXT NOT NULL,
    feedback TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    attempt_no INTEGER NOT NULL,
    candidate TEXT NOT NULL,
    mode TEXT NOT NULL,
    score REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_candidate ON attempts(candidate);
CREATE INDEX IF NOT EXISTS idx_attempts_job ON attempts(job_id);
CREATE INDEX IF NOT EXISTS idx_feedback_job ON human_feedback(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS leases (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_baselines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT REFERENCES jobs(id),
    repo TEXT,
    request TEXT NOT NULL,
    task_type TEXT NOT NULL,
    tool TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_tool ON manual_baselines(tool);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def acquire_lease(self, job_id: str, owner: str, ttl_seconds: int) -> bool:
        import time
        now_epoch = time.time()
        expires = now_epoch + ttl_seconds
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner, expires_at FROM leases WHERE job_id=?", (job_id,)).fetchone()
            if row and float(row[1]) > now_epoch and row[0] != owner:
                conn.rollback()
                return False
            conn.execute(
                """INSERT INTO leases(job_id, owner, expires_at, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
                (job_id, owner, expires, utcnow()),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def renew_lease(self, job_id: str, owner: str, ttl_seconds: int) -> bool:
        import time
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE leases SET expires_at=?, updated_at=? WHERE job_id=? AND owner=?",
                (time.time() + ttl_seconds, utcnow(), job_id, owner),
            )
            return cur.rowcount == 1

    def release_lease(self, job_id: str, owner: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM leases WHERE job_id=? AND owner=?", (job_id, owner))

    def recover_expired_leases(self, max_attempts: int) -> list[str]:
        import time
        recovered: list[str] = []
        now_epoch = time.time()
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT j.id FROM jobs j LEFT JOIN leases l ON l.job_id=j.id
                WHERE j.state IN ('RUNNING','VERIFYING','REVIEWING')
                AND (l.job_id IS NULL OR l.expires_at < ?)""",
                (now_epoch,),
            ).fetchall()
            for row in rows:
                job_id = row["id"]
                count = conn.execute("SELECT COUNT(*) n FROM attempts WHERE job_id=?", (job_id,)).fetchone()["n"]
                state = "FAILED" if int(count) >= max_attempts else "RETRY"
                conn.execute(
                    """UPDATE attempts SET status='EXECUTOR_FAILED', finished_at=?,
                    error=COALESCE(error,'controller lease expired; recovered after restart')
                    WHERE job_id=? AND status='RUNNING'""",
                    (utcnow(), job_id),
                )
                conn.execute("UPDATE jobs SET state=?, updated_at=? WHERE id=?", (state, utcnow(), job_id))
                conn.execute("DELETE FROM leases WHERE job_id=?", (job_id,))
                recovered.append(job_id)
        return recovered

    def next_job_id(self) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM jobs ORDER BY CAST(SUBSTR(id, 5) AS INTEGER) DESC LIMIT 1"
            ).fetchone()
        n = int(row["id"].split("-")[-1]) + 1 if row else 1
        return f"PMC-{n:06d}"

    def create_job(self, job: Job) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO jobs
                (id, repo, request, base_branch, priority, task_type,
                 acceptance_json, constraints_json, state, worktree,
                 baseline_commit, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.id,
                    str(job.repo),
                    job.request,
                    job.base_branch,
                    job.priority,
                    job.task_type,
                    json.dumps(job.acceptance),
                    json.dumps(job.constraints),
                    job.state.value,
                    str(job.worktree) if job.worktree else None,
                    job.baseline_commit,
                    now,
                    now,
                ),
            )

    def get_job(self, job_id: str) -> Job:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown job {job_id}")
        return Job(
            id=row["id"],
            repo=Path(row["repo"]),
            request=row["request"],
            base_branch=row["base_branch"],
            priority=row["priority"],
            task_type=row["task_type"],
            acceptance=json.loads(row["acceptance_json"]),
            constraints=json.loads(row["constraints_json"]),
            state=JobState(row["state"]),
            worktree=Path(row["worktree"]) if row["worktree"] else None,
            baseline_commit=row["baseline_commit"],
        )

    def update_job(self, job: Job) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE jobs SET state=?, worktree=?, baseline_commit=?, updated_at=?
                WHERE id=?""",
                (
                    job.state.value,
                    str(job.worktree) if job.worktree else None,
                    job.baseline_commit,
                    utcnow(),
                    job.id,
                ),
            )

    def set_state(self, job_id: str, state: JobState) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET state=?, updated_at=? WHERE id=?",
                (state.value, utcnow(), job_id),
            )

    def set_accepted_commit(self, job_id: str, commit: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET state=?, accepted_commit=?, updated_at=? WHERE id=?",
                (JobState.ACCEPTED.value, commit, utcnow(), job_id),
            )

    def list_jobs(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            )

    def queued_jobs(self, limit: int = 20) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id FROM jobs WHERE state IN (?, ?)
                ORDER BY priority ASC, created_at ASC LIMIT ?""",
                (JobState.QUEUED.value, JobState.RETRY.value, limit),
            ).fetchall()
        return [r["id"] for r in rows]

    def attempt_count(self, job_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) n FROM attempts WHERE job_id=?", (job_id,)
            ).fetchone()
        return int(row["n"])

    def begin_attempt(
        self,
        job_id: str,
        attempt_no: int,
        candidate: Any,
        selection_mode: str,
        selection_score: float,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO attempts
                (job_id, attempt_no, candidate, executor, model, role,
                 selection_mode, selection_score, status, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?)""",
                (
                    job_id,
                    attempt_no,
                    candidate.name,
                    candidate.executor,
                    candidate.model,
                    candidate.role,
                    selection_mode,
                    selection_score,
                    utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def finish_attempt(self, attempt_id: int, status: str, result: Any, duration: float) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE attempts SET status=?, finished_at=?, duration_seconds=?,
                input_tokens=?, output_tokens=?, cost_usd=?, provider_request_id=?,
                summary=?, error=?, raw_metrics_json=? WHERE id=?""",
                (
                    status,
                    utcnow(),
                    duration,
                    result.input_tokens,
                    result.output_tokens,
                    result.cost_usd,
                    result.provider_request_id,
                    result.summary,
                    result.error,
                    json.dumps(result.raw_metrics, default=str),
                    attempt_id,
                ),
            )

    def record_verification(self, attempt_id: int, v: VerificationResult) -> None:
        commands = [asdict(x) for x in v.commands]
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO verifications
                (attempt_id, ok, patch_lines, changed_files_json, scope_ok,
                 secret_scan_ok, protected_paths_ok, dependencies_ok,
                 findings_json, commands_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt_id,
                    int(v.ok),
                    v.patch_lines,
                    json.dumps(v.changed_files),
                    int(v.scope_ok),
                    int(v.secret_scan_ok),
                    int(v.protected_paths_ok),
                    int(v.dependencies_ok),
                    json.dumps(v.findings),
                    json.dumps(commands),
                    utcnow(),
                ),
            )

    def record_review(self, attempt_id: int, reviewer: str | None, review: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO reviews
                (attempt_id, reviewer_candidate, verdict, ok, summary, findings_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt_id,
                    reviewer,
                    review.verdict,
                    int(review.ok),
                    review.summary,
                    json.dumps(review.findings),
                    utcnow(),
                ),
            )

    def latest_ready_attempt_id(self, job_id: str) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM attempts WHERE job_id=? AND status='READY' ORDER BY attempt_no DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return int(row["id"]) if row else None

    def add_feedback(
        self, job_id: str, verdict: str, feedback: str | None = None, attempt_id: int | None = None
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO human_feedback(job_id, attempt_id, verdict, feedback, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, attempt_id, verdict, feedback, utcnow()),
            )

    def feedback_text(self, job_id: str) -> str:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT verdict, feedback FROM human_feedback WHERE job_id=? ORDER BY id",
                (job_id,),
            ).fetchall()
        parts = []
        for r in rows:
            if r["feedback"]:
                parts.append(f'{r["verdict"]}: {r["feedback"]}')
        return "\n".join(parts)

    def record_decision(self, job_id: str, attempt_no: int, d: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO scheduler_decisions
                (job_id, attempt_no, candidate, mode, score, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, attempt_no, d.candidate.name, d.mode, d.score, d.reason, utcnow()),
            )

    def candidate_stats(
        self, role: str = "builder", task_type: str | None = None, phase: str = "all",
        selection_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ["a.role=?", "a.finished_at IS NOT NULL"]
        params: list[Any] = [role]
        if task_type:
            where.append("j.task_type=?")
            params.append(task_type)
        if selection_mode:
            where.append("a.selection_mode=?")
            params.append(selection_mode)
        if phase == "first":
            where.append("a.attempt_no=1")
        elif phase == "repair":
            where.append("a.attempt_no>1")
        elif phase != "all":
            raise ValueError(f"unknown phase {phase}")
        sql = f"""
        SELECT a.candidate,
               COUNT(*) AS attempts,
               SUM(CASE WHEN v.ok=1 THEN 1 ELSE 0 END) AS verified,
               SUM(CASE WHEN EXISTS (
                   SELECT 1 FROM human_feedback hf
                   WHERE hf.attempt_id=a.id AND hf.verdict='ACCEPT'
               ) THEN 1 ELSE 0 END) AS accepted_attempts,
               SUM(CASE WHEN EXISTS (
                   SELECT 1 FROM human_feedback hf
                   WHERE hf.attempt_id=a.id AND hf.verdict IN ('ACCEPT','REJECT')
               ) THEN 1 ELSE 0 END) AS human_labeled,
               SUM(CASE WHEN EXISTS (
                   SELECT 1 FROM human_feedback hf
                   WHERE hf.attempt_id=a.id AND hf.verdict='REJECT'
               ) THEN 1 ELSE 0 END) AS human_rejected,
               AVG(a.duration_seconds) AS avg_seconds,
               AVG(COALESCE(a.cost_usd,0)) AS avg_cost,
               AVG(COALESCE(a.input_tokens,0)+COALESCE(a.output_tokens,0)) AS avg_tokens
        FROM attempts a
        JOIN jobs j ON j.id=a.job_id
        LEFT JOIN verifications v ON v.attempt_id=a.id
        WHERE {' AND '.join(where)}
        GROUP BY a.candidate
        ORDER BY verified * 1.0 / attempts DESC, attempts DESC
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def active_count_many(self, candidate_names: list[str]) -> int:
        if not candidate_names:
            return 0
        marks = ",".join("?" for _ in candidate_names)
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) n FROM attempts WHERE candidate IN ({marks}) AND status='RUNNING'",
                candidate_names,
            ).fetchone()
        return int(row["n"])

    def attempts_many_in_window(self, candidate_names: list[str], seconds: int) -> int:
        if not candidate_names:
            return 0
        marks = ",".join("?" for _ in candidate_names)
        params = [*candidate_names, f"-{seconds} seconds"]
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) n FROM attempts WHERE candidate IN ({marks}) "
                "AND julianday(started_at) >= julianday('now', ?)",
                params,
            ).fetchone()
        return int(row["n"])

    def tokens_many_in_window(self, candidate_names: list[str], seconds: int) -> int:
        if not candidate_names:
            return 0
        marks = ",".join("?" for _ in candidate_names)
        params = [*candidate_names, f"-{seconds} seconds"]
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)),0) n "
                f"FROM attempts WHERE candidate IN ({marks}) "
                "AND julianday(started_at) >= julianday('now', ?)",
                params,
            ).fetchone()
        return int(row["n"])

    def tokens_in_window(self, candidate_name: str, seconds: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)),0) n
                FROM attempts WHERE candidate=?
                AND julianday(started_at) >= julianday('now', ?)""",
                (candidate_name, f"-{seconds} seconds"),
            ).fetchone()
        return int(row["n"])

    def active_count(self, candidate_name: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) n FROM attempts WHERE candidate=? AND status='RUNNING'",
                (candidate_name,),
            ).fetchone()
        return int(row["n"])

    def attempts_in_window(self, candidate_name: str, seconds: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) n FROM attempts
                WHERE candidate=? AND julianday(started_at) >= julianday('now', ?)""",
                (candidate_name, f"-{seconds} seconds"),
            ).fetchone()
        return int(row["n"])

    def job_detail(self, job_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            attempts = conn.execute(
                "SELECT * FROM attempts WHERE job_id=? ORDER BY attempt_no", (job_id,)
            ).fetchall()
            feedback = conn.execute(
                "SELECT * FROM human_feedback WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
            verifications = conn.execute(
                """SELECT v.*, a.attempt_no, a.candidate FROM verifications v
                JOIN attempts a ON a.id=v.attempt_id
                WHERE a.job_id=? ORDER BY a.attempt_no""", (job_id,)
            ).fetchall()
            reviews = conn.execute(
                """SELECT r.*, a.attempt_no, a.candidate AS author_candidate FROM reviews r
                JOIN attempts a ON a.id=r.attempt_id
                WHERE a.job_id=? ORDER BY a.attempt_no""", (job_id,)
            ).fetchall()
        if not job:
            raise KeyError(job_id)
        return {
            "job": dict(job),
            "attempts": [dict(x) for x in attempts],
            "verifications": [dict(x) for x in verifications],
            "reviews": [dict(x) for x in reviews],
            "feedback": [dict(x) for x in feedback],
        }

    def record_manual_baseline(
        self, *, request: str, task_type: str, tool: str, duration_seconds: float,
        accepted: bool, cost_usd: float = 0.0, repo: str | None = None,
        job_id: str | None = None, notes: str | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO manual_baselines
                (job_id, repo, request, task_type, tool, duration_seconds, cost_usd, accepted, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, repo, request, task_type, tool, duration_seconds, cost_usd, int(accepted), notes, utcnow()),
            )
            return int(cur.lastrowid)

    def manual_stats(self, task_type: str | None = None) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if task_type:
            where.append("task_type=?")
            params.append(task_type)
        clause = "WHERE " + " AND ".join(where) if where else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT tool, COUNT(*) n, SUM(accepted) accepted,
                AVG(duration_seconds) avg_seconds, AVG(cost_usd) avg_cost
                FROM manual_baselines {clause}
                GROUP BY tool ORDER BY accepted * 1.0 / COUNT(*) DESC, n DESC""",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def export_attempts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT a.*, j.task_type, j.priority, j.state AS job_state,
                          v.ok AS verifier_ok, v.patch_lines, v.changed_files_json,
                          r.verdict AS reviewer_verdict, r.ok AS reviewer_ok
                   FROM attempts a
                   JOIN jobs j ON j.id=a.job_id
                   LEFT JOIN verifications v ON v.attempt_id=a.id
                   LEFT JOIN reviews r ON r.attempt_id=a.id
                   ORDER BY a.id"""
            ).fetchall()
        return [dict(r) for r in rows]
