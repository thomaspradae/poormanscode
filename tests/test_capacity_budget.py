from pathlib import Path

import pytest

from pmc.accounting import BudgetExceeded, ModelRequestAccounting
from pmc.budget import BUDGETS, characterize, envelope_for
from pmc.db import Database
from pmc.domain import Candidate, Job, ProviderCredential
from pmc.scheduler import Scheduler


def test_job_budget_roundtrip_and_risk_is_separate(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    complexity, risk = characterize("change one payment calculation", "TRIVIAL_EDIT")
    assert complexity == "STANDARD"  # risky one-file work is never treated as trivial
    assert risk == "HIGH"
    job = Job("PMC-X", tmp_path, "task", complexity="STANDARD", risk=risk,
              budget=envelope_for("STANDARD", risk))
    db.create_job(job)
    restored = db.get_job(job.id)
    assert restored.risk == "HIGH"
    assert restored.budget.name == "high-risk"


def test_provider_pool_selects_lanes_and_quarantines_auth_failure(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "pmc.db")
    db.create_job(Job("PMC-X", tmp_path, "task"))
    monkeypatch.setenv("POOL_KEY_1", "secret-one")
    monkeypatch.setenv("POOL_KEY_2", "secret-two")
    db.register_provider_credentials([
        ProviderCredential("pool-1", "pool", "POOL_KEY_1"),
        ProviderCredential("pool-2", "pool", "POOL_KEY_2"),
    ])
    first = db.reserve_provider_credential("pool", "PMC-X", 1, 100)
    second = db.reserve_provider_credential("pool", "PMC-X", 1, 100)
    assert first["credential_id"] != second["credential_id"]
    db.reconcile_provider_credential(first["reservation_id"], status_code=401,
                                     actual_tokens=0, headers={})
    assert db.provider_credential_env("pool") == second["api_key_env"]
    assert "secret-one" not in db.path.read_bytes().decode(errors="ignore")
    assert "secret-two" not in db.path.read_bytes().decode(errors="ignore")


def test_scheduler_conformance_gate(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    candidate = Candidate("c", "bash", model="m", base_url="http://invalid")
    scheduler = Scheduler(db, 0.1, 2, require_model_conformance=True)
    assert not scheduler.available(candidate).ok
    db.set_model_conformance(candidate, generation_ok=True, tool_ok=True, details={})
    assert scheduler.available(candidate).ok


def test_model_request_budget_is_a_hard_ceiling(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    db.create_job(Job("PMC-X", tmp_path, "task"))
    candidate = Candidate("c", "bash", model="m", base_url="http://invalid")
    attempt = db.begin_attempt("PMC-X", 1, candidate, "forced", 1.0)
    from dataclasses import replace
    budget = replace(BUDGETS["trivial"], max_model_requests=0)
    accounting = ModelRequestAccounting(db, job_id="PMC-X", attempt_id=attempt,
                                        candidate=candidate, budget=budget)
    with pytest.raises(BudgetExceeded, match="model-request"):
        accounting.reserve(1, [{"role": "user", "content": "hello"}], 10)
