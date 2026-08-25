from __future__ import annotations

import os
import re
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from ..domain import ExecutionRequest, ExecutionResult, Outcome


def _provider_failure(exc: Exception) -> tuple[Outcome, int | None, dict[str, str]]:
    """Classify provider failures crossing the remote OpenHands boundary.

    Agent Server exceptions are frequently reconstructed as generic SDK errors,
    so preserve structured HTTP information when present and use the message only
    as a conservative fallback. Credential values are never included.
    """
    message = str(exc)
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    if status is None:
        match = re.search(
            r"(?:HTTP(?: status)?\s*|status(?:_code)?[=: ]+)(\d{3})",
            message,
            re.IGNORECASE,
        )
        if match:
            status = int(match.group(1))
        elif re.search(r"\b429\b|RateLimit(?:Error|ed)", message, re.IGNORECASE):
            status = 429
        elif re.search(
            r"\b401\b|Unauthenticated|AuthenticationError", message, re.IGNORECASE
        ):
            status = 401
        elif re.search(r"\b403\b|PermissionDenied", message, re.IGNORECASE):
            status = 403
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    source_headers = getattr(response, "headers", None) or getattr(exc, "headers", None) or {}
    headers = {
        str(key).lower(): str(value)
        for key, value in dict(source_headers).items()
        if str(key).lower().startswith("x-ratelimit-")
        or str(key).lower() in {"retry-after", "request-id", "x-request-id"}
    }
    quota_exhausted = bool(
        re.search(r"(?:quota.*exhaust|exhaust.*quota|quota exceeded)", message, re.IGNORECASE)
    )
    if status == 429 or quota_exhausted:
        if quota_exhausted and "retry-after" not in headers:
            # Some ACP agents wrap a provider's daily-quota response as HTTP
            # 500 and omit reset metadata. Avoid immediately selecting the
            # same credential again while retaining a finite re-canary path.
            headers["retry-after"] = "3600"
        return Outcome.RATE_LIMIT, 429, headers
    if status in {401, 403} or (status is not None and status >= 500):
        return Outcome.PROVIDER_FAILURE, status, headers
    if "thought_signature" in message or "tool call" in message.lower():
        return Outcome.PROTOCOL_FAILURE, status, headers
    return Outcome.EXECUTOR_FAILURE, status, headers


class OpenHandsExecutor:
    name = "openhands"

    def _imports(self):
        try:
            from openhands.sdk import LLM, Conversation, Workspace
            from openhands.tools.preset.default import get_default_agent
            from pydantic import SecretStr

            return SecretStr, LLM, Conversation, Workspace, get_default_agent
        except ImportError as exc:
            raise RuntimeError(
                "OpenHands executor is not installed. Run: pip install -e '.[openhands]'"
            ) from exc

    def _acp_agent(self, candidate: Any):
        try:
            from openhands.sdk.agent import ACPAgent
        except ImportError as exc:
            raise RuntimeError(
                "OpenHands ACP support is unavailable; install a current openhands-sdk"
            ) from exc

        command = candidate.extra.get("acp_command")
        if not isinstance(command, list) or not all(
            isinstance(part, str) and part for part in command
        ):
            raise RuntimeError("OpenHands ACP candidate requires acp_command as a string list")
        return ACPAgent(
            acp_command=command,
            acp_server=candidate.extra.get("acp_server"),
            acp_model=candidate.extra.get("acp_model"),
            acp_session_mode=candidate.extra.get("acp_session_mode"),
            acp_isolate_data_dir=True,
        )

    def _metrics(
        self, llm: Any
    ) -> tuple[int | None, int | None, float | None, dict[str, Any]]:
        metrics = getattr(llm, "metrics", None)
        if metrics is None:
            return None, None, None, {}
        raw: dict[str, Any] = {}
        for name in (
            "accumulated_cost",
            "accumulated_token_usage",
            "token_usage",
            "latency",
        ):
            try:
                value = getattr(metrics, name)
                raw[name] = str(value)
            except Exception:
                pass
        cost = None
        try:
            cost = float(metrics.accumulated_cost)
        except Exception:
            pass
        # SDK metric structures evolve; retain raw metrics and best-effort common fields.
        input_tokens = output_tokens = None
        usage = getattr(metrics, "accumulated_token_usage", None) or getattr(
            metrics, "token_usage", None
        )
        for obj in [usage] if usage is not None else []:
            for attr, target in (
                ("prompt_tokens", "in"),
                ("input_tokens", "in"),
                ("completion_tokens", "out"),
                ("output_tokens", "out"),
            ):
                try:
                    val = int(getattr(obj, attr))
                except Exception:
                    continue
                if target == "in":
                    input_tokens = val
                else:
                    output_tokens = val
        return input_tokens, output_tokens, cost, raw

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        SecretStr, LLM, Conversation, Workspace, get_default_agent = self._imports()
        c = request.candidate
        if not c.model:
            return ExecutionResult(False, error="openhands executor requires model")
        key = os.getenv(c.api_key_env) if c.api_key_env else None
        # LiteLLM needs an explicit provider prefix for Gemini/OpenAI-compatible
        # endpoints, while PMC's provider conformance uses the endpoint-native
        # model id. Keep the adapter override candidate-local and observable.
        llm_model = c.extra.get("openhands_model") or c.model
        kwargs: dict[str, Any] = {"model": llm_model, "usage_id": f"pmc:{request.job.id}"}
        if "native_tool_calling" in c.extra:
            kwargs["native_tool_calling"] = bool(c.extra["native_tool_calling"])
        if key:
            kwargs["api_key"] = SecretStr(key)
        if c.base_url:
            kwargs["base_url"] = c.base_url
        if c.extra.get("agent_kind") == "acp":
            agent = self._acp_agent(c)
            llm = agent.llm
            secret_name = c.extra.get("acp_api_key_env")
            conversation_secrets = {secret_name: key} if key and secret_name else None
        else:
            llm = LLM(**kwargs)
            agent = get_default_agent(llm=llm, cli_mode=True)
            conversation_secrets = None
        try:
            if not c.server_url:
                if not c.extra.get("allow_local_unsandboxed", False):
                    return ExecutionResult(
                        False,
                        error=(
                            "local OpenHands execution is disabled by default because it is not a PMC sandbox. "
                            "Configure server_url or set allow_local_unsandboxed=true explicitly."
                        ),
                    )
                conversation = Conversation(
                    agent=agent,
                    workspace=str(request.worktree),
                    max_iteration_per_run=c.max_turns,
                    stuck_detection=True,
                    visualizer=None,
                    secrets=conversation_secrets,
                )
                try:
                    conversation.send_message(request.prompt)
                    conversation.run()
                finally:
                    conversation.close()
            else:
                # Remote Agent Server path. The controller ships a clean snapshot,
                # runs the agent remotely, then applies only the resulting Git patch locally.
                server_key = (
                    os.getenv(c.server_api_key_env)
                    if c.server_api_key_env
                    else None
                )
                workspace = Workspace(host=c.server_url, api_key=server_key)
                remote_dir = f"/tmp/pmc-{request.job.id.lower()}-{request.attempt_no}"
                with tempfile.TemporaryDirectory() as td:
                    archive = Path(td) / "repo.tar"
                    # Ship the CURRENT worktree so a remote executor can repair an earlier
                    # attempt. Exclude Git metadata; the remote copy gets its own baseline.
                    with tarfile.open(archive, "w") as tf:
                        import subprocess

                        listed = subprocess.run(
                            [
                                "git",
                                "-C",
                                str(request.worktree),
                                "ls-files",
                                "-co",
                                "--exclude-standard",
                            ],
                            text=True,
                            capture_output=True,
                            check=True,
                        ).stdout.splitlines()
                        for rel_text in listed:
                            path = request.worktree / rel_text
                            if path.exists() or path.is_symlink():
                                tf.add(path, arcname=rel_text, recursive=False)
                    remote_tar = (
                        f"/tmp/{request.job.id.lower()}-{request.attempt_no}.tar"
                    )
                    workspace.file_upload(str(archive), remote_tar)
                    workspace.execute_command(
                        f"rm -rf {remote_dir} && mkdir -p {remote_dir} && "
                        f"tar -xf {remote_tar} -C {remote_dir} && "
                        f"cd {remote_dir} && git init -q && git config user.email pmc@localhost && "
                        f"git config user.name PMC && git add -A && git commit --allow-empty -qm baseline"
                    )
                    remote_prompt = (
                        f"The repository for this task is at {remote_dir}. Begin every shell/file operation there.\n\n"
                        + request.prompt
                    )
                    conversation = Conversation(
                        agent=agent,
                        workspace=workspace,
                        max_iteration_per_run=c.max_turns,
                        stuck_detection=True,
                        visualizer=None,
                        secrets=conversation_secrets,
                    )
                    try:
                        conversation.send_message(remote_prompt)
                        conversation.run()
                        diff = workspace.execute_command(
                            f"cd {remote_dir} && git add -A && git diff --binary --cached HEAD --"
                        ).stdout
                    finally:
                        conversation.close()
                    if diff.strip():
                        from ..gitops import WorktreeManager

                        WorktreeManager(request.worktree.parent).apply_patch(
                            request.worktree, diff
                        )
            in_tok, out_tok, cost, raw = self._metrics(llm)
            return ExecutionResult(
                True,
                summary="OpenHands conversation completed; controller will verify independently",
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                raw_metrics={**raw, "accounting": "sdk-aggregate"},
                accounting_level="aggregate",
            )
        except Exception as exc:
            in_tok, out_tok, cost, raw = self._metrics(llm)
            outcome, provider_status, rate_headers = _provider_failure(exc)
            if provider_status is not None:
                raw["provider_status"] = provider_status
            if rate_headers:
                raw["rate_headers"] = rate_headers
            return ExecutionResult(
                False,
                error=f"OpenHands failed: {type(exc).__name__}: {exc}",
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                raw_metrics={**raw, "accounting": "sdk-aggregate"},
                outcome=outcome,
                accounting_level="aggregate",
            )
