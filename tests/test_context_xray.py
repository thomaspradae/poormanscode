from pathlib import Path

import pytest

from pmc.accounting import ContextCapacityExceeded, ModelRequestAccounting
from pmc.context_xray import analyze_request_context
from pmc.db import Database
from pmc.domain import Candidate, ExecutionResult, Job, ProviderCredential
from pmc.providers.openai_compat import ChatReply


def test_xray_counts_tool_schema_and_never_retains_content():
    secret_marker = "sensitive-ticket-text"
    metrics = analyze_request_context(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": secret_marker},
            {"role": "tool", "content": "large output", "tool_call_id": "x"},
        ],
        [{"type": "function", "function": {"name": "shell", "description": "x" * 200}}],
        model_context_window=1000,
    )
    assert metrics["composition"]["tool_schemas"] > 0
    assert metrics["composition"]["tool_observations"] > 0
    assert metrics["context_occupancy"] is not None
    assert secret_marker not in str(metrics)


def test_xray_detects_unchanged_retry_and_growth():
    messages = [{"role": "user", "content": "task"}]
    first = analyze_request_context(messages)
    second = analyze_request_context(
        messages,
        previous_estimated_input=first["estimated_input_tokens"],
        previous_request_hash=first["request_hash"],
    )
    assert second["unchanged_from_previous"] is True
    assert second["growth_tokens"] == 0


def test_accounting_persists_xray_and_actual_estimation_delta(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    job = Job("PMC-XRAY", tmp_path, "task")
    candidate = Candidate("xray", "bash", model="model")
    db.create_job(job)
    attempt = db.begin_attempt(job.id, 1, candidate, "forced", 0)
    accounting = ModelRequestAccounting(
        db, job_id=job.id, attempt_id=attempt, candidate=candidate
    )
    ticket = accounting.reserve(
        1,
        [{"role": "user", "content": "task"}],
        100,
        tools=[{"type": "function", "function": {"name": "shell"}}],
    )
    accounting.succeed(ticket, ChatReply("ok", input_tokens=12, output_tokens=3))
    row = db.model_request_xray(job.id)[0]
    assert row["context_metrics"]["actual_input_tokens"] == 12
    assert "estimation_error_tokens" in row["context_metrics"]
    assert row["context_metrics"]["composition"]["tool_schemas"] > 0


def test_request_soft_limit_rejects_before_provider_dispatch(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    job = Job("PMC-CAP", tmp_path, "task")
    candidate = Candidate.from_mapping(
        {
            "name": "capacity",
            "executor": "bash",
            "model": "model",
            "request_token_soft_limit": 20,
        }
    )
    db.create_job(job)
    attempt = db.begin_attempt(job.id, 1, candidate, "forced", 0)
    accounting = ModelRequestAccounting(
        db, job_id=job.id, attempt_id=attempt, candidate=candidate
    )
    with pytest.raises(ContextCapacityExceeded):
        accounting.reserve(1, [{"role": "user", "content": "x" * 200}], 10)
    assert db.model_request_xray(job.id) == []
    assert "MODEL_REQUEST_INCOMPATIBLE_CONTEXT" in {
        event["event_type"] for event in db.job_events(job.id)
    }


def test_profiles_keep_thresholds_hypotheses_and_report_scope_evidence(
    tmp_path: Path, monkeypatch
):
    db = Database(tmp_path / "db.sqlite")
    job = Job("PMC-PROFILE", tmp_path, "task")
    candidate = Candidate(
        "profile", "bash", provider="pool", model="model", max_turns=3
    )
    db.create_job(job)
    monkeypatch.setenv("POOL_1", "one")
    monkeypatch.setenv("POOL_2", "two")
    db.register_provider_credentials(
        [
            ProviderCredential("pool-1", "pool", "POOL_1"),
            ProviderCredential("pool-2", "pool", "POOL_2"),
        ]
    )
    attempt = db.begin_attempt(job.id, 1, candidate, "forced", 0)
    accounting = ModelRequestAccounting(
        db, job_id=job.id, attempt_id=attempt, candidate=candidate
    )
    for turn in (1, 2):
        ticket = accounting.reserve(
            turn,
            [{"role": "user", "content": "summarize" if turn == 1 else "act"}],
            100,
            tools=[] if turn == 1 else [{"type": "function", "name": "shell"}],
        )
        accounting.succeed(
            ticket,
            ChatReply(
                "ok",
                input_tokens=500 + turn,
                output_tokens=10,
                rate_headers={
                    "x-ratelimit-limit-tokens": "25000",
                    "x-ratelimit-remaining-tokens": "24000",
                },
            ),
        )
    db.finish_attempt(
        attempt,
        "EXECUTOR_FAILED",
        ExecutionResult(False, error="bounded diagnostic"),
        1.0,
        outcome="EXECUTOR_FAILURE",
    )
    profile = db.context_profiles("profile")[0]
    assert profile["policy_status"] == "INITIAL_HYPOTHESIS"
    assert profile["automatic_threshold_tuning"] is False
    assert profile["successful_input_tokens_p50"] is not None
    scope = db.quota_scope_evidence("pool")[0]
    assert scope["credentials_configured"] == 2
    assert scope["configured_quota_scopes"] == 0
    # Known-good lanes are preferred over UNKNOWN lanes during ordinary work;
    # explicit credential probes warm the remaining lane without contaminating
    # this model-request history.
    assert scope["counter_evidence_lanes"] == 1
    assert scope["observed_independence_confidence"] == "OBSERVED"
