from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pmc.domain import Candidate, ExecutionRequest, Job
from pmc.executors.jules import JulesExecutor
from pmc.gitops import WorktreeManager


class Response:
    def __init__(self, data): self.data = data
    def raise_for_status(self): return None
    def json(self): return self.data


class FakeClient:
    def __init__(self, patch): self.patch = patch
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def post(self, url, json):
        return Response({"name": "sessions/mock", "id": "mock", "state": "COMPLETED"})
    def get(self, url, params=None):
        if url.endswith("/activities"):
            return Response({"activities": [{"artifacts": [{"changeSet": {"gitPatch": {
                "baseCommitId": "abc",
                "unidiffPatch": self.patch,
                "suggestedCommitMessage": "fix",
            }}}]}]})
        return Response({"id": "mock", "state": "COMPLETED"})


def sh(cwd: Path, cmd: str):
    return subprocess.run(["bash", "-lc", cmd], cwd=cwd, check=True, text=True, capture_output=True)


def test_jules_changeset_applies_to_controller_worktree(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"; repo.mkdir()
    sh(repo, "git init -q -b main && git config user.email t@e.com && git config user.name T")
    (repo / "x.txt").write_text("old\n")
    sh(repo, "git add -A && git commit -qm init")
    wm = WorktreeManager(tmp_path / "w")
    wt, baseline = wm.create(repo, "PMC-000001", "main")
    (wt / "x.txt").write_text("new\n")
    patch = wm.diff(wt, baseline)
    sh(wt, "git checkout -- x.txt")

    c = Candidate(name="j", executor="jules", api_key_env="JULES_API_KEY", source="sources/github/o/r")
    job = Job("PMC-000001", repo, "change it", baseline_commit=baseline, worktree=wt)
    req = ExecutionRequest(job, c, wt, "change it", 1)
    ex = JulesExecutor()
    monkeypatch.setattr(ex, "_client", lambda _: FakeClient(patch))
    monkeypatch.setenv("JULES_API_KEY", "fake")
    result = ex.run(req)
    assert result.ok
    assert (wt / "x.txt").read_text() == "new\n"
