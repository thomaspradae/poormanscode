from __future__ import annotations

import subprocess
from pathlib import Path

from pmc.domain import Candidate, ExecutionRequest, Job
from pmc.executors.jules import JulesExecutor
from pmc.gitops import WorktreeManager


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FakeClient:
    def __init__(self, patch, state="COMPLETED"):
        self.patch = patch
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url, json):
        self.posted = json
        return Response({"name": "sessions/mock", "id": "mock", "state": self.state})

    def get(self, url, params=None):
        if url.endswith("/activities"):
            return Response(
                {
                    "activities": [
                        {
                            "artifacts": [
                                {
                                    "changeSet": {
                                        "gitPatch": {
                                            "baseCommitId": "abc",
                                            "unidiffPatch": self.patch,
                                            "suggestedCommitMessage": "fix",
                                        }
                                    }
                                }
                            ]
                        }
                    ]
                }
            )
        return Response({"id": "mock", "state": self.state})


def sh(cwd: Path, cmd: str):
    return subprocess.run(
        ["bash", "-lc", cmd], cwd=cwd, check=True, text=True, capture_output=True
    )


def test_jules_changeset_applies_to_controller_worktree(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(
        repo,
        "git init -q -b main && git config user.email t@e.com && git config user.name T",
    )
    (repo / "x.txt").write_text("old\n")
    sh(repo, "git add -A && git commit -qm init")
    wm = WorktreeManager(tmp_path / "w")
    wt, baseline = wm.create(repo, "PMC-000001", "main")
    (wt / "x.txt").write_text("new\n")
    patch = wm.diff(wt, baseline)
    sh(wt, "git checkout -- x.txt")

    c = Candidate(
        name="j",
        executor="jules",
        api_key_env="JULES_API_KEY",
        source="sources/github/o/r",
    )
    job = Job("PMC-000001", repo, "change it", baseline_commit=baseline, worktree=wt)
    req = ExecutionRequest(job, c, wt, "change it", 1)
    ex = JulesExecutor()
    monkeypatch.setattr(ex, "_client", lambda _: FakeClient(patch))
    monkeypatch.setenv("JULES_API_KEY", "fake")
    result = ex.run(req)
    assert result.ok
    assert (wt / "x.txt").read_text() == "new\n"


def test_jules_awaiting_user_feedback_is_a_completed_patch(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(
        repo,
        "git init -q -b main && git config user.email t@e.com && git config user.name T",
    )
    (repo / "x.txt").write_text("old\n")
    sh(repo, "git add -A && git commit -qm init")
    wm = WorktreeManager(tmp_path / "w")
    wt, baseline = wm.create(repo, "PMC-000010", "main")
    (wt / "x.txt").write_text("new\n")
    patch = wm.diff(wt, baseline)
    sh(wt, "git checkout -- x.txt")
    candidate = Candidate(
        name="j", executor="jules", api_key_env="JULES_API_KEY", source="sources/github/o/r"
    )
    job = Job("PMC-000010", repo, "change it", baseline_commit=baseline, worktree=wt)
    executor = JulesExecutor()
    monkeypatch.setattr(executor, "_client", lambda _: FakeClient(patch, "AWAITING_USER_FEEDBACK"))
    monkeypatch.setenv("JULES_API_KEY", "fake")

    progress: list[tuple[str, str]] = []
    request = ExecutionRequest(job, candidate, wt, "change it", 1)
    request.on_progress = lambda session_id, state: progress.append((session_id, state))
    result = executor.run(request)

    assert result.ok
    assert result.provider_request_id == "mock"
    assert progress == [("mock", "AWAITING_USER_FEEDBACK")]
    assert (wt / "x.txt").read_text() == "new\n"


def test_jules_derives_source_from_job_github_origin(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(
        repo,
        "git init -q -b main && git config user.email t@e.com && git config user.name T",
    )
    (repo / "x.txt").write_text("old\n")
    sh(repo, "git add -A && git commit -qm init")
    sh(repo, "git remote add origin git@github.com:owner/project.git")
    wm = WorktreeManager(tmp_path / "w")
    wt, baseline = wm.create(repo, "PMC-000002", "main")
    (wt / "x.txt").write_text("new\n")
    patch = wm.diff(wt, baseline)
    sh(wt, "git checkout -- x.txt")

    client = FakeClient(patch)
    candidate = Candidate(
        name="j",
        executor="jules",
        api_key_env="JULES_API_KEY",
        source="auto",
    )
    job = Job("PMC-000002", repo, "change it", baseline_commit=baseline, worktree=wt)
    request = ExecutionRequest(job, candidate, wt, "change it", 1)
    executor = JulesExecutor()
    monkeypatch.setattr(executor, "_client", lambda _: client)
    monkeypatch.setenv("JULES_API_KEY", "fake")

    result = executor.run(request)

    assert result.ok
    assert (
        client.posted["sourceContext"]["source"]
        == "sources/github/owner/project"
    )


def test_jules_explicit_source_override_wins(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    candidate = Candidate(
        name="j",
        executor="jules",
        source="sources/github/other/pinned",
    )
    job = Job("PMC-000003", repo, "change it")
    request = ExecutionRequest(job, candidate, repo, "change it", 1)

    assert JulesExecutor._source_for(request) == "sources/github/other/pinned"


def test_jules_uses_pushed_task_branch_for_follow_up_lane(tmp_path: Path):
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    sh(remote.parent, f"git init -q --bare {remote}")
    sh(
        repo,
        "git init -q -b main && git config user.email t@e.com && git config user.name T",
    )
    (repo / "x.txt").write_text("x\n")
    sh(repo, f"git add -A && git commit -qm init && git remote add origin {remote} && git push -qu origin main")
    wm = WorktreeManager(tmp_path / "w")
    wt, baseline = wm.create(repo, "PMC-000011", "main")
    sh(wt, "git push -qu origin pmc/pmc-000011")
    candidate = Candidate(name="j", executor="jules", source="sources/github/o/r")
    job = Job("PMC-000011", repo, "continue", baseline_commit=baseline, worktree=wt)

    assert JulesExecutor._starting_branch(ExecutionRequest(job, candidate, wt, "continue", 2)) == "pmc/pmc-000011"


def test_jules_maps_pinned_checkpoint_to_remote_branch(tmp_path: Path):
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    sh(remote.parent, f"git init -q --bare {remote}")
    sh(
        repo,
        "git init -q -b main && git config user.email t@e.com && git config user.name T",
    )
    (repo / "x.txt").write_text("main\n")
    sh(repo, f"git add -A && git commit -qm init && git remote add origin {remote} && git push -qu origin main")
    sh(repo, "git checkout -qb pmc/checkpoint && echo checkpoint > x.txt && git add x.txt && git commit -qm checkpoint && git push -qu origin pmc/checkpoint")
    wm = WorktreeManager(tmp_path / "w")
    wt, baseline = wm.create(repo, "PMC-000012", "pmc/checkpoint")
    candidate = Candidate(name="j", executor="jules", source="sources/github/o/r")
    job = Job(
        "PMC-000012", repo, "continue", base_branch=baseline, baseline_commit=baseline, worktree=wt
    )

    assert JulesExecutor._starting_branch(ExecutionRequest(job, candidate, wt, "continue", 1)) == "pmc/checkpoint"
