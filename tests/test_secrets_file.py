import os
from pathlib import Path

import pytest

from pmc.config import load_config, load_secrets_file


def test_secure_secrets_file_loads_literal_values(tmp_path: Path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text("# comment\nPMC_TEST_SECRET='literal value'\n")
    path.chmod(0o600)
    monkeypatch.delenv("PMC_TEST_SECRET", raising=False)
    assert load_secrets_file(path) == ["PMC_TEST_SECRET"]
    assert os.environ["PMC_TEST_SECRET"] == "literal value"


def test_secrets_file_rejects_open_permissions(tmp_path: Path):
    path = tmp_path / "secrets.env"
    path.write_text("PMC_TEST_SECRET=value\n")
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="mode 600"):
        load_secrets_file(path)


def test_load_config_auto_registers_numbered_provider_keys(tmp_path: Path, monkeypatch):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("GROQ_API_KEY_2=secret-two\nGROQ_API_KEY_9=secret-nine\n")
    secrets.chmod(0o600)
    config = tmp_path / "config.toml"
    config.write_text(
        """
[pmc]
db_path = "./pmc.db"
runs_dir = "./runs"
worktrees_dir = "./worktrees"

[[providers.groq.credentials]]
id = "groq-2"
api_key_env = "GROQ_API_KEY_2"
""".strip()
    )
    monkeypatch.delenv("GROQ_API_KEY_2", raising=False)
    monkeypatch.delenv("GROQ_API_KEY_9", raising=False)

    cfg = load_config(config)

    assert [
        (item.id, item.api_key_env) for item in cfg.provider_credentials["groq"]
    ] == [
        ("groq-2", "GROQ_API_KEY_2"),
        ("groq-9", "GROQ_API_KEY_9"),
    ]
