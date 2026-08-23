import subprocess
from pathlib import Path

from pmc.gitops import WorktreeManager, resolve_commit


def _git(repo: Path, command: str):
    subprocess.run(command, cwd=repo, shell=True, check=True, text=True)


def test_commit_is_idempotent_across_acceptance_crash(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(
        repo,
        "git init -q -b main && git config user.name Test && git config user.email t@example.com",
    )
    (repo / "a").write_text("one")
    _git(repo, "git add a && git commit -qm baseline")
    baseline = resolve_commit(repo, "HEAD")
    (repo / "a").write_text("two")
    manager = WorktreeManager(tmp_path / "worktrees")
    first = manager.commit_idempotent(repo, baseline, "change", "PMC-000001")
    second = manager.commit_idempotent(repo, baseline, "change", "PMC-000001")
    assert first == second
    assert (
        subprocess.run(
            "git rev-list --count HEAD",
            cwd=repo,
            shell=True,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        == "2"
    )
