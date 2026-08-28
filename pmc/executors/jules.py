from __future__ import annotations

import os
import re
import time

import httpx

from ..domain import ExecutionRequest, ExecutionResult, Outcome
from ..gitops import WorktreeManager, resolve_commit


class JulesExecutor:
    name = "jules"
    BASE = "https://jules.googleapis.com/v1alpha"

    @staticmethod
    def _http_error(exc: httpx.HTTPStatusError) -> str:
        """Return provider diagnostics without headers, request data, or secrets."""
        response = exc.response
        detail = ""
        try:
            body = response.json()
            error = body.get("error", {}) if isinstance(body, dict) else {}
            if isinstance(error, dict):
                status = error.get("status")
                message = error.get("message")
                detail = " ".join(str(x) for x in (status, message) if x)
        except (ValueError, TypeError):
            pass
        suffix = f": {detail}" if detail else ""
        return f"Jules HTTP {response.status_code}{suffix}"

    def _client(self, env_name: str) -> httpx.Client:
        key = os.getenv(env_name)
        if not key:
            raise RuntimeError(f"missing {env_name}")
        return httpx.Client(
            headers={"x-goog-api-key": key, "Accept": "application/json"},
            timeout=60,
        )

    @staticmethod
    def _source_for(request: ExecutionRequest) -> str:
        """Resolve the Jules source from the job repo unless explicitly pinned."""
        configured = request.candidate.source
        if configured and configured != "auto":
            return configured

        from ..gitops import git

        result = git(request.job.repo, "remote", "get-url", "origin", check=False)
        remote = result.stdout.strip()
        patterns = (
            r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
            r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
            r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        )
        for pattern in patterns:
            match = re.match(pattern, remote)
            if match:
                return f"sources/github/{match.group('owner')}/{match.group('repo')}"
        raise RuntimeError(
            "Jules requires a GitHub origin remote or an explicit source override"
        )

    @staticmethod
    def _starting_branch(request: ExecutionRequest) -> str:
        """Choose the task branch so a follow-up Jules lane sees its checkpoint."""
        from ..gitops import git

        # A resumed PMC task has a controller-owned worktree branch such as
        # ``pmc/PMC-000052``.  Once that branch is pushed, it is the canonical
        # handoff point between independent Jules sessions/credentials.  Using
        # the original base branch here made every later lane start from main
        # and regenerate already-integrated foundation files.
        active = git(request.worktree, "branch", "--show-current", check=False)
        branch = active.stdout.strip()
        if branch:
            remote = git(
                request.worktree,
                "ls-remote",
                "--heads",
                "origin",
                f"refs/heads/{branch}",
                check=False,
            )
            if remote.returncode == 0 and remote.stdout.strip():
                return branch

        configured = request.job.base_branch
        if configured and not re.fullmatch(r"[0-9a-fA-F]{7,64}", configured):
            return configured
        head = git(request.job.repo, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
        match = re.search(r"refs/remotes/origin/(.+)$", head.stdout.strip())
        return match.group(1) if match else "main"

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        c = request.candidate
        if request.attempt_no > 1:
            from ..gitops import git

            baseline = request.job.baseline_commit or "HEAD"
            if (
                git(
                    request.worktree, "diff", "--quiet", baseline, "--", check=False
                ).returncode
                != 0
            ):
                return ExecutionResult(
                    False,
                    error="Jules cannot safely repair an unpushed local patch; use it as a first attempt or push an explicit branch",
                )
        if not c.api_key_env:
            return ExecutionResult(False, error="Jules candidate requires api_key_env")
        try:
            source = self._source_for(request)
            with self._client(c.api_key_env) as client:
                payload = {
                    "title": f"{request.job.id}: {request.job.request[:80]}",
                    "prompt": request.prompt,
                    "sourceContext": {
                        "source": source,
                        "githubRepoContext": {
                            "startingBranch": self._starting_branch(request)
                        },
                    },
                    "requirePlanApproval": False,
                }
                r = client.post(f"{self.BASE}/sessions", json=payload)
                r.raise_for_status()
                session = r.json()
                session_name = session.get("name") or f"sessions/{session['id']}"
                session_id = session_name.split("/")[-1]
                poll = float(c.extra.get("poll_seconds", 10))
                deadline = time.monotonic() + float(
                    c.extra.get("timeout_seconds", 3600)
                )
                state = session.get("state", "QUEUED")
                # Jules commonly ends a successful autonomous session in
                # AWAITING_USER_FEEDBACK: the agent has produced its patch and
                # is waiting for a human reply in the Jules UI.  PMC does not
                # need a conversational reply here; it must retrieve the
                # immutable ChangeSet immediately.  Treating that state as
                # non-terminal previously left completed work polling until
                # the local one-hour deadline, then falsely reported TIMEOUT.
                terminal_success = {"COMPLETED", "AWAITING_USER_FEEDBACK"}
                while state not in terminal_success | {"FAILED"}:
                    if time.monotonic() >= deadline:
                        return ExecutionResult(
                            False,
                            error="Jules session timed out",
                            provider_request_id=session_id,
                            outcome=Outcome.TIMEOUT,
                            raw_metrics={"accounting": "unknown"},
                        )
                    time.sleep(poll)
                    r = client.get(f"{self.BASE}/sessions/{session_id}")
                    r.raise_for_status()
                    session = r.json()
                    state = session.get("state", "")
                if state == "FAILED":
                    return ExecutionResult(
                        False,
                        error="Jules session failed",
                        provider_request_id=session_id,
                        raw_metrics={"session": session},
                    )

                # Activities are immutable and carry the ChangeSet git patch.
                r = client.get(
                    f"{self.BASE}/sessions/{session_id}/activities",
                    params={"pageSize": 100},
                )
                r.raise_for_status()
                activities = r.json().get("activities", [])
                patches: list[dict] = []
                for activity in activities:
                    for artifact in activity.get("artifacts", []) or []:
                        cs = artifact.get("changeSet")
                        if cs and cs.get("gitPatch", {}).get("unidiffPatch"):
                            patches.append(cs["gitPatch"])
                if not patches:
                    return ExecutionResult(
                        False,
                        error="Jules completed without a ChangeSet patch",
                        provider_request_id=session_id,
                        raw_metrics={"state": state},
                    )
                patch = patches[-1]
                local_base = request.job.baseline_commit or resolve_commit(
                    request.worktree, "HEAD"
                )
                remote_base = patch.get("baseCommitId")
                manager = WorktreeManager(request.worktree.parent)
                manager.apply_patch(request.worktree, patch["unidiffPatch"])
                return ExecutionResult(
                    True,
                    summary=patch.get("suggestedCommitMessage")
                    or "Jules patch retrieved",
                    provider_request_id=session_id,
                    raw_metrics={
                        "accounting": "unknown",
                        "jules_session": session_id,
                        "remote_base": remote_base,
                        "local_base": local_base,
                        "base_match": bool(
                            remote_base and local_base.startswith(remote_base)
                        )
                        or bool(remote_base and remote_base.startswith(local_base)),
                    },
                )
        except httpx.HTTPStatusError as exc:
            return ExecutionResult(
                False,
                error=self._http_error(exc),
                outcome=Outcome.PROVIDER_FAILURE,
                raw_metrics={"accounting": "unknown"},
            )
        except Exception as exc:  # noqa: BLE001 - normalize provider/transport failures
            return ExecutionResult(
                False,
                error=f"Jules failed: {type(exc).__name__}: {exc}",
                outcome=Outcome.PROVIDER_FAILURE,
                raw_metrics={"accounting": "unknown"},
            )
