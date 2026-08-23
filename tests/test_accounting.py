from pathlib import Path

from pmc.accounting import ModelRequestAccounting
from pmc.db import Database
from pmc.domain import Candidate, Job
from pmc.providers import ProviderError


def test_model_request_rate_limit_reconciles_and_blocks_routing(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    job = Job("PMC-000001", tmp_path, "task")
    candidate = Candidate(
        name="provider-model-bash-v1",
        executor="bash",
        provider="provider",
        model="model",
    )
    db.create_job(job)
    attempt = db.begin_attempt(job.id, 1, candidate, "forced", 0)
    accounting = ModelRequestAccounting(
        db, job_id=job.id, attempt_id=attempt, candidate=candidate
    )
    ticket = accounting.reserve(1, [{"role": "user", "content": "hello"}], 100)
    accounting.fail(
        ticket,
        ProviderError(
            429, "limited", {"retry-after": "60", "x-ratelimit-remaining-tokens": "0"}
        ),
    )
    with db.connect() as conn:
        request = conn.execute("SELECT * FROM model_requests").fetchone()
        reservation = conn.execute("SELECT * FROM quota_reservations").fetchone()
    assert request["state"] == "RATE_LIMITED"
    assert reservation["state"] == "RECONCILED"
    assert db.quota_availability(candidate.name)[0] is False
    events = [row["event_type"] for row in db.job_events(job.id)]
    assert "MODEL_REQUEST_RATE_LIMITED" in events
    assert "MODEL_REQUEST_RECONCILED" in events
