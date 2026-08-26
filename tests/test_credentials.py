from pathlib import Path

from pmc.config import PMCConfig
from pmc.credentials import probe_credentials
from pmc.db import Database
from pmc.domain import Candidate, ProviderCredential
from pmc.providers import ChatReply, ProviderError


def _config(tmp_path: Path) -> PMCConfig:
    return PMCConfig(
        db_path=tmp_path / "pmc.db",
        runs_dir=tmp_path / "runs",
        worktrees_dir=tmp_path / "worktrees",
        candidates=[
            Candidate(
                "provider-openhands",
                "openhands",
                provider="provider",
                model="provider/model",
                base_url="https://provider.invalid/v1",
            )
        ],
    )


def test_credential_probes_update_lane_health_without_model_quality(
    tmp_path: Path, monkeypatch
):
    cfg = _config(tmp_path)
    db = Database(cfg.db_path)
    credentials = [
        ProviderCredential(f"lane-{number}", "provider", f"PROBE_KEY_{number}")
        for number in range(1, 4)
    ]
    db.register_provider_credentials(credentials)
    for number in range(1, 4):
        monkeypatch.setenv(f"PROBE_KEY_{number}", f"secret-{number}")

    class FakeClient:
        def __init__(self, _base_url, api_key_env, timeout):
            del timeout
            self.api_key_env = api_key_env

        def chat(self, **_kwargs):
            if self.api_key_env == "PROBE_KEY_2":
                raise ProviderError(401, "provider HTTP 401", {})
            if self.api_key_env == "PROBE_KEY_3":
                raise ProviderError(429, "provider HTTP 429", {"retry-after": "2"})
            return ChatReply(
                "PMC_CREDENTIAL_OK",
                input_tokens=5,
                output_tokens=2,
                request_id="safe-request-id",
                rate_headers={"x-ratelimit-remaining-tokens": "95"},
            )

    monkeypatch.setattr("pmc.credentials.OpenAICompatibleClient", FakeClient)
    results = probe_credentials(db, cfg, concurrency=1)

    assert [result.health for result in results] == [
        "AVAILABLE",
        "AUTH_FAILED",
        "RATE_LIMITED",
    ]
    with db.connect() as conn:
        probes = conn.execute(
            "SELECT state,http_status FROM credential_probes ORDER BY credential_id"
        ).fetchall()
        conformance = conn.execute("SELECT COUNT(*) FROM model_conformance").fetchone()[
            0
        ]
        events = conn.execute(
            "SELECT event_type FROM events WHERE event_type LIKE 'CREDENTIAL_PROBE_%'"
        ).fetchall()
    assert [(row["state"], row["http_status"]) for row in probes] == [
        ("SUCCEEDED", 200),
        ("FAILED", 401),
        ("FAILED", 429),
    ]
    assert conformance == 0
    assert len(events) == 6
    database_text = db.path.read_bytes().decode(errors="ignore")
    assert "secret-1" not in database_text
    assert "secret-2" not in database_text
    assert "secret-3" not in database_text


def test_default_probe_only_targets_unknown_lanes(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    db = Database(cfg.db_path)
    db.register_provider_credentials(
        [ProviderCredential("lane-1", "provider", "PROBE_KEY_1")]
    )
    monkeypatch.setenv("PROBE_KEY_1", "secret")

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def chat(self, **_kwargs):
            return ChatReply("ok")

    monkeypatch.setattr("pmc.credentials.OpenAICompatibleClient", FakeClient)
    assert len(probe_credentials(db, cfg, concurrency=1)) == 1
    assert probe_credentials(db, cfg, concurrency=1) == []
