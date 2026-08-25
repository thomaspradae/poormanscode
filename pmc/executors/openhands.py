from __future__ import annotations

import os
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from ..domain import ExecutionRequest, ExecutionResult, Outcome


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
        llm = LLM(**kwargs)
        agent = get_default_agent(llm=llm, cli_mode=True)
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
                        f"git config user.name PMC && git add -A && git commit -qm baseline"
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
                    )
                    try:
                        conversation.send_message(remote_prompt)
                        conversation.run()
                        diff = workspace.execute_command(
                            f"cd {remote_dir} && git diff --binary HEAD --"
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
            return ExecutionResult(
                False,
                error=f"OpenHands failed: {type(exc).__name__}: {exc}",
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                raw_metrics={**raw, "accounting": "sdk-aggregate"},
                outcome=Outcome.EXECUTOR_FAILURE,
                accounting_level="aggregate",
            )
