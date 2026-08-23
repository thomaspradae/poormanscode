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


def _header_int(payload: dict[str, Any], name: str) -> int | None:
    try:
        return int(payload[name]) if name in payload else None
    except (TypeError, ValueError):
        return None


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

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    job_id TEXT,
    attempt_id INTEGER,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL,
    migrated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_versions (
    candidate TEXT NOT NULL, version TEXT NOT NULL, fingerprint TEXT NOT NULL,
    registered_at TEXT NOT NULL, PRIMARY KEY(candidate, version)
);

CREATE TABLE IF NOT EXISTS quota_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL, candidate TEXT NOT NULL, event_type TEXT NOT NULL,
    requests INTEGER, tokens INTEGER, cost_usd REAL, reset_at TEXT,
    payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate TEXT NOT NULL, job_id TEXT NOT NULL, estimated_tokens INTEGER NOT NULL,
    state TEXT NOT NULL, actual_tokens INTEGER, created_at TEXT NOT NULL, reconciled_at TEXT
);
CREATE TABLE IF NOT EXISTS quota_state (
    candidate TEXT PRIMARY KEY, blocked_until REAL, remaining_requests INTEGER,
    remaining_tokens INTEGER, reset_at TEXT, updated_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL, attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    candidate TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    turn_number INTEGER NOT NULL, state TEXT NOT NULL,
    estimated_input_tokens INTEGER, estimated_output_tokens INTEGER,
    actual_input_tokens INTEGER, actual_output_tokens INTEGER,
    estimated_cost_usd REAL, actual_cost_usd REAL, provider_request_id TEXT,
    rate_headers_json TEXT NOT NULL DEFAULT '{}', error TEXT,
    started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_requests_attempt ON model_requests(attempt_id, turn_number);

CREATE TABLE IF NOT EXISTS resource_leases (
    resource_key TEXT NOT NULL, job_id TEXT NOT NULL, attempt_id INTEGER,
    owner TEXT NOT NULL, expires_at REAL NOT NULL, snapshot_json TEXT NOT NULL,
    PRIMARY KEY(resource_key, job_id)
);

CREATE INDEX IF NOT EXISTS idx_attempts_candidate ON attempts(candidate);
CREATE INDEX IF NOT EXISTS idx_attempts_job ON attempts(job_id);
CREATE INDEX IF NOT EXISTS idx_feedback_job ON human_feedback(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, seq);
CREATE INDEX IF NOT EXISTS idx_quota_candidate ON quota_events(candidate, occurred_at);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

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
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        from .versioning import SCHEMA_VERSION

        additions = {
            "attempts": {
                "outcome": "TEXT",
                "version_snapshot_json": "TEXT",
                "context_hash": "TEXT",
                "context_manifest_json": "TEXT",
            },
            "scheduler_decisions": {
                "eligible_json": "TEXT",
                "unavailable_json": "TEXT",
                "selection_probability": "REAL",
                "policy_version": "TEXT",
                "snapshot_json": "TEXT",
            },
        }
        for table, columns in additions.items():
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        conn.execute(
            "INSERT INTO schema_metadata(singleton,schema_version,migrated_at) VALUES(1,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET schema_version=excluded.schema_version,migrated_at=excluded.migrated_at",
            (SCHEMA_VERSION, utcnow()),
        )

    def event(
        self,
        event_type: str,
        *,
        job_id: str | None = None,
        attempt_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        import uuid

        event_id = f"evt-{uuid.uuid4().hex}"
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(event_id,job_id,attempt_id,event_type,payload_json,occurred_at) VALUES(?,?,?,?,?,?)",
                (
                    event_id,
                    job_id,
                    attempt_id,
                    event_type,
                    json.dumps(payload or {}, default=str),
                    utcnow(),
                ),
            )
        return event_id

    def reserve_quota(self, candidate: str, job_id: str, estimated_tokens: int) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO quota_reservations(candidate,job_id,estimated_tokens,state,created_at) VALUES(?,?,?,'RESERVED',?)",
                (candidate, job_id, estimated_tokens, utcnow()),
            )
            reservation_id = int(cur.lastrowid)
        self.event(
            "QUOTA_RESERVED",
            job_id=job_id,
            payload={"candidate": candidate, "estimated_tokens": estimated_tokens},
        )
        return reservation_id

    def reserve_model_request(
        self,
        *,
        request_key: str,
        job_id: str,
        attempt_id: int,
        candidate: Any,
        turn_number: int,
        estimated_input: int,
        estimated_output: int,
        estimated_cost: float | None,
    ) -> tuple[int, int]:
        reservation_id = self.reserve_quota(
            candidate.name, job_id, estimated_input + estimated_output
        )
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO model_requests(request_key,job_id,attempt_id,candidate,provider,model,
                turn_number,state,estimated_input_tokens,estimated_output_tokens,estimated_cost_usd,created_at)
                VALUES(?,?,?,?,?,?,?,'RESERVED',?,?,?,?)""",
                (
                    request_key,
                    job_id,
                    attempt_id,
                    candidate.name,
                    candidate.provider or candidate.quota_group or candidate.executor,
                    candidate.model or "unknown",
                    turn_number,
                    estimated_input,
                    estimated_output,
                    estimated_cost,
                    utcnow(),
                ),
            )
            model_request_id = int(cur.lastrowid)
        self.event(
            "MODEL_REQUEST_RESERVED",
            job_id=job_id,
            attempt_id=attempt_id,
            payload={
                "model_request_id": model_request_id,
                "request_key": request_key,
                "provider": candidate.provider or candidate.quota_group,
                "model": candidate.model,
                "estimated_input_tokens": estimated_input,
                "estimated_output_tokens": estimated_output,
            },
        )
        return model_request_id, reservation_id

    def start_model_request(
        self, model_request_id: int, job_id: str, attempt_id: int
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE model_requests SET state='STARTED',started_at=? WHERE id=?",
                (utcnow(), model_request_id),
            )
        self.event(
            "MODEL_REQUEST_STARTED",
            job_id=job_id,
            attempt_id=attempt_id,
            payload={"model_request_id": model_request_id},
        )

    def finish_model_request(
        self,
        *,
        model_request_id: int,
        reservation_id: int,
        job_id: str,
        attempt_id: int,
        candidate: Any,
        reply: Any | None = None,
        error: Any | None = None,
    ) -> None:
        headers = dict(
            getattr(reply, "rate_headers", {})
            or getattr(error, "rate_headers", {})
            or {}
        )
        input_tokens = getattr(reply, "input_tokens", None)
        output_tokens = getattr(reply, "output_tokens", None)
        cost = getattr(reply, "cost_usd", None)
        request_id = getattr(reply, "request_id", None)
        status = getattr(error, "status_code", None)
        state = (
            "SUCCEEDED"
            if error is None
            else ("RATE_LIMITED" if status == 429 else "FAILED")
        )
        with self.connect() as conn:
            conn.execute(
                """UPDATE model_requests SET state=?,actual_input_tokens=?,actual_output_tokens=?,
                actual_cost_usd=?,provider_request_id=?,rate_headers_json=?,error=?,finished_at=? WHERE id=?""",
                (
                    state,
                    input_tokens,
                    output_tokens,
                    cost,
                    request_id,
                    json.dumps(headers),
                    str(error) if error else None,
                    utcnow(),
                    model_request_id,
                ),
            )
        provider = candidate.provider or candidate.quota_group or candidate.executor
        self.reconcile_quota(
            reservation_id,
            provider=provider,
            candidate=candidate.name,
            actual_tokens=(input_tokens or 0) + (output_tokens or 0),
            cost_usd=cost,
            payload=headers,
        )
        if headers or error is not None:
            self.record_quota_event(
                provider,
                candidate.name,
                "RATE_LIMIT" if status == 429 else "MODEL_REQUEST",
                headers,
            )
        event = (
            "MODEL_REQUEST_SUCCEEDED"
            if error is None
            else (
                "MODEL_REQUEST_RATE_LIMITED"
                if status == 429
                else "MODEL_REQUEST_FAILED"
            )
        )
        self.event(
            event,
            job_id=job_id,
            attempt_id=attempt_id,
            payload={
                "model_request_id": model_request_id,
                "request_id": request_id,
                "actual_input_tokens": input_tokens,
                "actual_output_tokens": output_tokens,
                "actual_cost_usd": cost,
                "rate_headers": headers,
                "error": str(error) if error else None,
            },
        )
        self.event(
            "MODEL_REQUEST_RECONCILED",
            job_id=job_id,
            attempt_id=attempt_id,
            payload={
                "model_request_id": model_request_id,
                "reservation_id": reservation_id,
            },
        )

    def model_request_totals(self, attempt_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) requests,SUM(actual_input_tokens) input_tokens,
                SUM(actual_output_tokens) output_tokens,SUM(actual_cost_usd) cost_usd
                FROM model_requests WHERE attempt_id=?""",
                (attempt_id,),
            ).fetchone()
        return dict(row)

    def register_candidate(self, candidate: Any) -> None:
        from .versioning import stable_hash

        fingerprint = stable_hash(candidate)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT fingerprint FROM candidate_versions WHERE candidate=? AND version=?",
                (candidate.name, candidate.version),
            ).fetchone()
            if row and row["fingerprint"] != fingerprint:
                raise RuntimeError(
                    f"candidate {candidate.name} version {candidate.version} changed; increment its version"
                )
            conn.execute(
                "INSERT OR IGNORE INTO candidate_versions(candidate,version,fingerprint,registered_at) VALUES(?,?,?,?)",
                (candidate.name, candidate.version, fingerprint, utcnow()),
            )

    def reconcile_quota(
        self,
        reservation_id: int,
        *,
        provider: str,
        candidate: str,
        actual_tokens: int,
        cost_usd: float | None,
        payload: dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE quota_reservations SET state='RECONCILED',actual_tokens=?,reconciled_at=? WHERE id=?",
                (actual_tokens, utcnow(), reservation_id),
            )
            conn.execute(
                "INSERT INTO quota_events(provider,candidate,event_type,requests,tokens,cost_usd,payload_json,occurred_at) VALUES(?,?,'RECONCILED',1,?,?,?,?)",
                (
                    provider,
                    candidate,
                    actual_tokens,
                    cost_usd,
                    json.dumps(payload, default=str),
                    utcnow(),
                ),
            )

    def record_quota_event(
        self, provider: str, candidate: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        import time

        retry_after = payload.get("retry-after")
        try:
            blocked_until = (
                time.time() + float(retry_after) if retry_after is not None else None
            )
        except (TypeError, ValueError):
            blocked_until = None
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO quota_events(provider,candidate,event_type,payload_json,occurred_at,reset_at) VALUES(?,?,?,?,?,?)",
                (
                    provider,
                    candidate,
                    event_type,
                    json.dumps(payload, default=str),
                    utcnow(),
                    payload.get("x-ratelimit-reset") or payload.get("retry-after"),
                ),
            )
            conn.execute(
                """INSERT INTO quota_state(candidate,blocked_until,remaining_requests,remaining_tokens,reset_at,updated_at,snapshot_json)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(candidate) DO UPDATE SET
                blocked_until=excluded.blocked_until, remaining_requests=excluded.remaining_requests,
                remaining_tokens=excluded.remaining_tokens, reset_at=excluded.reset_at,
                updated_at=excluded.updated_at, snapshot_json=excluded.snapshot_json""",
                (
                    candidate,
                    blocked_until,
                    _header_int(payload, "x-ratelimit-remaining-requests"),
                    _header_int(payload, "x-ratelimit-remaining-tokens"),
                    payload.get("x-ratelimit-reset") or retry_after,
                    utcnow(),
                    json.dumps(payload, default=str),
                ),
            )

    def quota_availability(self, candidate: str) -> tuple[bool, str]:
        import time

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM quota_state WHERE candidate=?", (candidate,)
            ).fetchone()
        if not row:
            return True, ""
        if row["blocked_until"] and float(row["blocked_until"]) > time.time():
            return False, f"provider cooldown until {row['blocked_until']}"
        if row["remaining_requests"] == 0 or row["remaining_tokens"] == 0:
            return False, f"provider quota exhausted; reset={row['reset_at']}"
        return True, ""

    def job_events(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM events WHERE job_id=? ORDER BY seq", (job_id,)
                )
            ]

    def reserved_tokens(self, candidate_names: list[str]) -> int:
        if not candidate_names:
            return 0
        marks = ",".join("?" for _ in candidate_names)
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(estimated_tokens),0) n FROM quota_reservations WHERE state='RESERVED' AND candidate IN ({marks})",
                candidate_names,
            ).fetchone()
        return int(row["n"])

    def reserve_resource(
        self,
        resource_key: str,
        job_id: str,
        attempt_id: int,
        owner: str,
        ttl_seconds: int,
        snapshot: dict[str, Any],
    ) -> bool:
        import time

        with self.connect() as conn:
            conn.execute(
                "DELETE FROM resource_leases WHERE expires_at < ?", (time.time(),)
            )
            rows = conn.execute(
                "SELECT snapshot_json FROM resource_leases WHERE resource_key=?",
                (resource_key,),
            ).fetchall()
            requested = snapshot.get("requested") or {}
            capacity = snapshot.get("capacity") or {}
            if requested and capacity:
                used: dict[str, float] = {}
                for row in rows:
                    prior = json.loads(row[0]).get("requested", {})
                    for name, amount in prior.items():
                        used[name] = used.get(name, 0) + float(amount)
                if any(
                    used.get(name, 0) + float(amount) > float(capacity.get(name, 0))
                    for name, amount in requested.items()
                ):
                    return False
            elif rows:
                return False
            conn.execute(
                "INSERT INTO resource_leases(resource_key,job_id,attempt_id,owner,expires_at,snapshot_json) VALUES(?,?,?,?,?,?)",
                (
                    resource_key,
                    job_id,
                    attempt_id,
                    owner,
                    time.time() + ttl_seconds,
                    json.dumps(snapshot, default=str),
                ),
            )
        self.event(
            "RESOURCE_RESERVED",
            job_id=job_id,
            attempt_id=attempt_id,
            payload={"resource_key": resource_key, **snapshot},
        )
        return True

    def quantitative_resource_available(
        self, resource_key: str, requested: dict[str, float], capacity: dict[str, float]
    ) -> tuple[bool, str]:
        import time

        with self.connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_json FROM resource_leases WHERE resource_key=? AND expires_at>=?",
                (resource_key, time.time()),
            ).fetchall()
        used: dict[str, float] = {}
        for row in rows:
            for name, amount in json.loads(row[0]).get("requested", {}).items():
                used[name] = used.get(name, 0) + float(amount)
        for name, amount in requested.items():
            if used.get(name, 0) + float(amount) > float(capacity.get(name, 0)):
                return False, f"resource {resource_key} insufficient {name}"
        return True, ""

    def release_resource(self, resource_key: str, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM resource_leases WHERE resource_key=? AND job_id=?",
                (resource_key, job_id),
            )

    def resource_busy(self, resource_key: str) -> bool:
        import time

        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM resource_leases WHERE resource_key=? AND expires_at>=?",
                (resource_key, time.time()),
            ).fetchone()
        return bool(row)

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
            row = conn.execute(
                "SELECT owner, expires_at FROM leases WHERE job_id=?", (job_id,)
            ).fetchone()
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
            conn.execute(
                "DELETE FROM leases WHERE job_id=? AND owner=?", (job_id, owner)
            )

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
                attempt_ids = [
                    int(r[0])
                    for r in conn.execute(
                        "SELECT id FROM attempts WHERE job_id=? AND status='RUNNING'",
                        (job_id,),
                    )
                ]
                count = conn.execute(
                    "SELECT COUNT(*) n FROM attempts WHERE job_id=?", (job_id,)
                ).fetchone()["n"]
                state = "FAILED" if int(count) >= max_attempts else "RETRY"
                conn.execute(
                    """UPDATE attempts SET status='EXECUTOR_FAILED', finished_at=?,
                    error=COALESCE(error,'controller lease expired; recovered after restart'),
                    outcome='RESOURCE_FAILURE'
                    WHERE job_id=? AND status='RUNNING'""",
                    (utcnow(), job_id),
                )
                conn.execute(
                    "UPDATE jobs SET state=?, updated_at=? WHERE id=?",
                    (state, utcnow(), job_id),
                )
                conn.execute("DELETE FROM resource_leases WHERE job_id=?", (job_id,))
                conn.execute(
                    """UPDATE quota_reservations SET state='RECONCILED',actual_tokens=0,reconciled_at=?
                    WHERE job_id=? AND state='RESERVED'""",
                    (utcnow(), job_id),
                )
                if attempt_ids:
                    marks = ",".join("?" for _ in attempt_ids)
                    conn.execute(
                        f"""UPDATE model_requests SET state='FAILED',error=COALESCE(error,
                        'controller lease expired'),finished_at=? WHERE attempt_id IN ({marks})
                        AND state IN ('RESERVED','STARTED')""",
                        (utcnow(), *attempt_ids),
                    )
                conn.execute("DELETE FROM leases WHERE job_id=?", (job_id,))
                recovered.append(job_id)
        for job_id in recovered:
            self.event(
                "JOB_RECOVERED",
                job_id=job_id,
                payload={"reason": "controller lease expired"},
            )
        return recovered

    def cancel_running_attempts(self, job_id: str) -> list[int]:
        with self.connect() as conn:
            ids = [
                int(r[0])
                for r in conn.execute(
                    "SELECT id FROM attempts WHERE job_id=? AND status='RUNNING'",
                    (job_id,),
                )
            ]
            conn.execute(
                "UPDATE attempts SET status='CANCELLED',outcome='CANCELLED',finished_at=? WHERE job_id=? AND status='RUNNING'",
                (utcnow(), job_id),
            )
        for attempt_id in ids:
            self.event("ATTEMPT_CANCELLED", job_id=job_id, attempt_id=attempt_id)
        return ids

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
        self.event(
            "JOB_CREATED",
            job_id=job.id,
            payload={"repo": str(job.repo), "request": job.request},
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
        *,
        version_snapshot: dict[str, Any] | None = None,
        context_hash: str | None = None,
        context_manifest: dict[str, Any] | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO attempts
                (job_id, attempt_no, candidate, executor, model, role,
                 selection_mode, selection_score, status, started_at,
                 version_snapshot_json, context_hash, context_manifest_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?, ?)""",
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
                    json.dumps(version_snapshot or {}, sort_keys=True),
                    context_hash,
                    json.dumps(context_manifest or {}, sort_keys=True),
                ),
            )
            attempt_id = int(cur.lastrowid)
        self.event(
            "ATTEMPT_STARTED",
            job_id=job_id,
            attempt_id=attempt_id,
            payload={"attempt_number": attempt_no, "candidate": candidate.name},
        )
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: int,
        status: str,
        result: Any,
        duration: float,
        outcome: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE attempts SET status=?, finished_at=?, duration_seconds=?,
                input_tokens=?, output_tokens=?, cost_usd=?, provider_request_id=?,
                summary=?, error=?, raw_metrics_json=?, outcome=? WHERE id=?""",
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
                    outcome or status,
                    attempt_id,
                ),
            )
            row = conn.execute(
                "SELECT job_id FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        self.event(
            "EXECUTOR_FINISHED",
            job_id=row["job_id"],
            attempt_id=attempt_id,
            payload={
                "status": status,
                "outcome": outcome or status,
                "duration_seconds": duration,
            },
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
        self,
        job_id: str,
        verdict: str,
        feedback: str | None = None,
        attempt_id: int | None = None,
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
                parts.append(f"{r['verdict']}: {r['feedback']}")
        return "\n".join(parts)

    def record_decision(self, job_id: str, attempt_no: int, d: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO scheduler_decisions
                (job_id, attempt_no, candidate, mode, score, reason, created_at,
                 eligible_json, unavailable_json, selection_probability, policy_version, snapshot_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    attempt_no,
                    d.candidate.name,
                    d.mode,
                    d.score,
                    d.reason,
                    utcnow(),
                    json.dumps(d.eligible),
                    json.dumps(d.unavailable),
                    d.selection_probability,
                    d.policy_version,
                    json.dumps(d.snapshot, default=str),
                ),
            )
        self.event(
            "SCHEDULER_DECISION",
            job_id=job_id,
            payload={
                "attempt_number": attempt_no,
                "chosen": d.candidate.name,
                "mode": d.mode,
                "score": d.score,
                "eligible": d.eligible,
                "unavailable": d.unavailable,
                "selection_probability": d.selection_probability,
                "policy_version": d.policy_version,
                "snapshot": d.snapshot,
            },
        )

    def candidate_stats(
        self,
        role: str = "builder",
        task_type: str | None = None,
        phase: str = "all",
        selection_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        where = [
            "a.role=?",
            "a.finished_at IS NOT NULL",
            "COALESCE(a.outcome,'') NOT IN "
            "('PROVIDER_FAILURE','RATE_LIMIT','RESOURCE_FAILURE','EXECUTOR_FAILURE',"
            "'EXECUTOR_CRASH','TIMEOUT','CANCELLED')",
        ]
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
        WHERE {" AND ".join(where)}
        GROUP BY a.candidate
        ORDER BY verified * 1.0 / attempts DESC, attempts DESC
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def efficiency_stats(self) -> list[dict[str, Any]]:
        """Headline human-accepted success and wall-clock metrics by candidate."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT a.candidate, a.job_id, j.created_at, hf.created_at accepted_at,
                CASE WHEN hf.id IS NULL THEN 0 ELSE 1 END accepted
                FROM attempts a JOIN jobs j ON j.id=a.job_id
                LEFT JOIN human_feedback hf ON hf.attempt_id=a.id AND hf.verdict='ACCEPT'
                WHERE a.role='builder' AND a.finished_at IS NOT NULL"""
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = grouped.setdefault(
                row["candidate"], {"attempts": 0, "accepted": 0, "seconds": []}
            )
            item["attempts"] += 1
            if row["accepted"]:
                item["accepted"] += 1
                start = datetime.fromisoformat(row["created_at"])
                end = datetime.fromisoformat(row["accepted_at"])
                item["seconds"].append((end - start).total_seconds())
        result = []
        for candidate, item in sorted(grouped.items()):
            seconds = sorted(item.pop("seconds"))
            n = len(seconds)
            median = (
                None
                if not n
                else (
                    seconds[n // 2]
                    if n % 2
                    else (seconds[n // 2 - 1] + seconds[n // 2]) / 2
                )
            )
            result.append(
                {
                    "candidate": candidate,
                    **item,
                    "success_rate": item["accepted"] / item["attempts"],
                    "median_wall_clock_to_accepted_seconds": median,
                }
            )
        return result

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
                WHERE a.job_id=? ORDER BY a.attempt_no""",
                (job_id,),
            ).fetchall()
            reviews = conn.execute(
                """SELECT r.*, a.attempt_no, a.candidate AS author_candidate FROM reviews r
                JOIN attempts a ON a.id=r.attempt_id
                WHERE a.job_id=? ORDER BY a.attempt_no""",
                (job_id,),
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
        self,
        *,
        request: str,
        task_type: str,
        tool: str,
        duration_seconds: float,
        accepted: bool,
        cost_usd: float = 0.0,
        repo: str | None = None,
        job_id: str | None = None,
        notes: str | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO manual_baselines
                (job_id, repo, request, task_type, tool, duration_seconds, cost_usd, accepted, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    repo,
                    request,
                    task_type,
                    tool,
                    duration_seconds,
                    cost_usd,
                    int(accepted),
                    notes,
                    utcnow(),
                ),
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
