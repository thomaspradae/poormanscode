from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pmc.sandbox import SandboxLimits, build_sandbox, scrubbed_environment


def test_scrubbed_environment_removes_controller_credentials(monkeypatch):
    monkeypatch.setenv("PMC_TEST_API_KEY", "must-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    env = scrubbed_environment()
    assert "PMC_TEST_API_KEY" not in env
    assert "SSH_AUTH_SOCK" not in env


def test_guarded_sandbox_enforces_file_size_limit(tmp_path: Path):
    result = build_sandbox("guarded").run(
        tmp_path,
        "python -c \"open('large','wb').write(b'x'*200000)\"",
        env=scrubbed_environment(),
        network=False,
        limits=SandboxLimits(wall_seconds=5, cpu_seconds=5, file_bytes=1024),
    )
    assert result.returncode != 0


@pytest.mark.skipif(
    subprocess.run(["id", "pmc-worker"], capture_output=True, check=False).returncode
    != 0,
    reason="pmc-worker is not provisioned",
)
def test_restricted_worker_does_not_receive_secrets(tmp_path: Path, monkeypatch):
    subprocess.run(["setfacl", "-Rm", "u:pmc-worker:rwX", str(tmp_path)], check=True)
    monkeypatch.setenv("PMC_TEST_API_KEY", "must-not-leak")
    result = build_sandbox("restricted-user").run(
        tmp_path,
        'test -z "${PMC_TEST_API_KEY:-}" && touch worker-proof',
        env=scrubbed_environment(),
        network=True,
        limits=SandboxLimits(wall_seconds=5),
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "worker-proof").exists()
