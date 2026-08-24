import subprocess
from pathlib import Path

from pmc.context import build_context_bundle


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_skeletal_repo_gets_bootstrap_preflight(tmp_path: Path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "AGENTS.md").write_text("# Instructions\n")
    (tmp_path / "poorman.yaml").write_text("test: null\n")
    _git(tmp_path, "add", "AGENTS.md", "poorman.yaml")
    _git(tmp_path, "commit", "-qm", "init")

    bundle = build_context_bundle(tmp_path, "Create the first application")

    assert bundle.manifest["repository_state"] == "SKELETAL"
    assert "BOOTSTRAP PREFLIGHT" in bundle.content
    assert "create the required project structure" in bundle.content


def test_established_repo_does_not_get_bootstrap_preflight(tmp_path: Path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-qm", "init")

    bundle = build_context_bundle(tmp_path, "Change the application")

    assert bundle.manifest["repository_state"] == "ESTABLISHED"
    assert "BOOTSTRAP PREFLIGHT" not in bundle.content


def test_context_does_not_excerpt_lockfiles_or_binary_assets(tmp_path: Path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "package-lock.json").write_text('{"config": "large generated data"}')
    (tmp_path / "config.ico").write_bytes(b"\x00\x01config")
    (tmp_path / "config.ts").write_text("export const config = true;\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")

    bundle = build_context_bundle(tmp_path, "Implement config")

    assert bundle.manifest["excerpt_files"] == ["config.ts"]
    assert "large generated data" not in bundle.content
