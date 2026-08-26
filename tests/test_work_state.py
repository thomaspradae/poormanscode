import subprocess
from pathlib import Path

from pmc.domain import Job
from pmc.work_state import WORK_STATE_VERSION, build_work_state


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_work_state_preserves_repository_truth_without_agent_history(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("value = 1\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "baseline")
    baseline = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "app.py").write_text("value = 2\n")
    (repo / "new.py").write_text("created = True\n")
    job = Job(
        "PMC-STATE",
        repo,
        "change value",
        acceptance=["tests pass"],
        baseline_commit=baseline,
        worktree=repo,
    )
    state = build_work_state(
        job,
        repo,
        failures=["Attempt 1 verification:\nexpected 2"],
        current_plan=["repair app.py", "run tests"],
    )
    assert state.version == WORK_STATE_VERSION
    assert state.changed_files == ["app.py", "new.py"]
    assert "value = 2" in state.current_diff
    assert "created = True" in state.current_diff
    assert "expected 2" in state.prompt_packet()
    assert "private reasoning" not in state.prompt_packet()
    assert state.manifest()["has_partial_work"] is True
    assert (
        state.content_hash
        == build_work_state(
            job,
            repo,
            failures=["Attempt 1 verification:\nexpected 2"],
            current_plan=["repair app.py", "run tests"],
        ).content_hash
    )
