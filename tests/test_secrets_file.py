import os
from pathlib import Path

import pytest

from pmc.config import load_secrets_file


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
