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


def test_allow_empty_acceptance_commit_is_explicit_and_idempotent(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(
        repo,
        "git init -q -b main && git config user.name Test && git config user.email t@example.com",
    )
    (repo / "a").write_text("one")
    _git(repo, "git add a && git commit -qm baseline")
    baseline = resolve_commit(repo, "HEAD")
    manager = WorktreeManager(tmp_path / "worktrees")
    first = manager.commit_idempotent(
        repo,
        baseline,
        "integration verified",
        "PMC-000002",
        allow_empty=True,
    )
    second = manager.commit_idempotent(
        repo,
        baseline,
        "integration verified",
        "PMC-000002",
        allow_empty=True,
    )
    assert first == second
    assert "PMC-Job: PMC-000002" in subprocess.run(
        "git log -1 --format=%B",
        cwd=repo,
        shell=True,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
