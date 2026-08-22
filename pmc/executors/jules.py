from __future__ import annotations

import os
import time

import httpx

from ..domain import ExecutionRequest, ExecutionResult
from ..gitops import WorktreeManager, resolve_commit


class JulesExecutor:
    name = "jules"
    BASE = "https://jules.googleapis.com/v1alpha"

    def _client(self, env_name: str) -> httpx.Client:
        key = os.getenv(env_name)
        if not key:
            raise RuntimeError(f"missing {env_name}")
        return httpx.Client(
            headers={"x-goog-api-key": key, "Accept": "application/json"},
            timeout=60,
        )

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        c = request.candidate
        if request.attempt_no > 1:
            from ..gitops import git
            baseline = request.job.baseline_commit or "HEAD"
            if git(request.worktree, "diff", "--quiet", baseline, "--", check=False).returncode != 0:
                return ExecutionResult(
                    False,
                    error="Jules cannot safely repair an unpushed local patch; use it as a first attempt or push an explicit branch",
                )
        if not c.source:
            return ExecutionResult(False, error="Jules candidate requires source=...")
        if not c.api_key_env:
            return ExecutionResult(False, error="Jules candidate requires api_key_env")
        try:
            with self._client(c.api_key_env) as client:
                payload = {
                    "title": f"{request.job.id}: {request.job.request[:80]}",
                    "prompt": request.prompt,
                    "sourceContext": {
                        "source": c.source,
                        "githubRepoContext": {"startingBranch": request.job.base_branch},
                    },
                    "requirePlanApproval": False,
                }
                r = client.post(f"{self.BASE}/sessions", json=payload)
                r.raise_for_status()
                session = r.json()
                session_name = session.get("name") or f"sessions/{session['id']}"
                session_id = session_name.split("/")[-1]
                poll = float(c.extra.get("poll_seconds", 10))
                deadline = time.monotonic() + float(c.extra.get("timeout_seconds", 3600))
                state = session.get("state", "QUEUED")
                while state not in {"COMPLETED", "FAILED"}:
                    if time.monotonic() >= deadline:
                        return ExecutionResult(False, error="Jules session timed out", provider_request_id=session_id)
                    time.sleep(poll)
                    r = client.get(f"{self.BASE}/sessions/{session_id}")
                    r.raise_for_status()
                    session = r.json()
                    state = session.get("state", "")
                if state == "FAILED":
                    return ExecutionResult(False, error="Jules session failed", provider_request_id=session_id, raw_metrics={"session": session})

                # Activities are immutable and carry the ChangeSet git patch.
                r = client.get(f"{self.BASE}/sessions/{session_id}/activities", params={"pageSize": 100})
                r.raise_for_status()
                activities = r.json().get("activities", [])
                patches: list[dict] = []
                for activity in activities:
                    for artifact in activity.get("artifacts", []) or []:
                        cs = artifact.get("changeSet")
                        if cs and cs.get("gitPatch", {}).get("unidiffPatch"):
                            patches.append(cs["gitPatch"])
                if not patches:
                    return ExecutionResult(False, error="Jules completed without a ChangeSet patch", provider_request_id=session_id, raw_metrics={"state": state})
                patch = patches[-1]
                local_base = request.job.baseline_commit or resolve_commit(request.worktree, "HEAD")
                remote_base = patch.get("baseCommitId")
                manager = WorktreeManager(request.worktree.parent)
                manager.apply_patch(request.worktree, patch["unidiffPatch"])
                return ExecutionResult(
                    True,
                    summary=patch.get("suggestedCommitMessage") or "Jules patch retrieved",
                    provider_request_id=session_id,
                    raw_metrics={
                        "jules_session": session_id,
                        "remote_base": remote_base,
                        "local_base": local_base,
                        "base_match": bool(remote_base and local_base.startswith(remote_base)) or bool(remote_base and remote_base.startswith(local_base)),
                    },
                )
        except Exception as exc:
            return ExecutionResult(False, error=f"Jules failed: {type(exc).__name__}: {exc}")
