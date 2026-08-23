from pathlib import Path

import httpx

from pmc.db import Database
from pmc.domain import Candidate, Job
from pmc.research import ResearchService


def test_grounded_research_is_persisted_without_credential(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "pmc.db")
    job = Job("PMC-000001", tmp_path, "research")
    db.create_job(job)
    attempt = db.begin_attempt(job.id, 1, Candidate("worker", "bash"), "forced", 0)
    monkeypatch.setenv("TEST_GEMINI_KEY", "not-recorded")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "steps": [
                    {"type": "google_search_call"},
                    {
                        "type": "model_output",
                        "text": "Use the official tool.",
                        "annotations": [
                            {"url": "https://example.test/docs", "title": "Docs"}
                        ],
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())
    service = ResearchService(
        db,
        job_id=job.id,
        attempt_id=attempt,
        model="gemini-test",
        api_key_env="TEST_GEMINI_KEY",
        max_queries=1,
    )
    result = service.search("official docs")
    assert result.search_queries == 1
    assert result.sources[0]["url"] == "https://example.test/docs"
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM research_requests").fetchone()
        serialized = str(dict(row))
    assert row["state"] == "SUCCEEDED"
    assert "not-recorded" not in serialized
