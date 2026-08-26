from __future__ import annotations

import os
import re
import subprocess
import tarfile
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import PrivateAttr

from ..domain import ExecutionRequest, ExecutionResult, Outcome
from ..providers.openai_compat import ChatReply
from ..versioning import stable_hash


def _event_fingerprint(event: Any) -> str:
    try:
        payload = event.model_dump(mode="json")
    except Exception:
        payload = {"type": type(event).__name__, "id": getattr(event, "id", None)}
    return stable_hash({"type": type(event).__name__, "payload": payload})


def _agent_progress_diagnostics(
    events: list[Any], diff: str, base: dict[str, Any]
) -> dict[str, Any]:
    """Content-free progress signals around condensation and tool activity."""
    names = [type(event).__name__ for event in events]
    condensation_positions = [
        index for index, name in enumerate(names) if name == "Condensation"
    ]
    action_positions = [
        (index, _event_fingerprint(event))
        for index, (event, name) in enumerate(zip(events, names, strict=True))
        if name == "ActionEvent"
    ]
    action_hashes = [fingerprint for _, fingerprint in action_positions]
    last_condensation = condensation_positions[-1] if condensation_positions else None
    post_condensation = [
        fingerprint
        for index, fingerprint in action_positions
        if last_condensation is not None and index > last_condensation
    ]
    changed_files = sorted(
        set(re.findall(r"^diff --git a/(.+?) b/", diff, flags=re.MULTILINE))
    )
    return {
        **base,
        "event_counts": dict(Counter(names)),
        "condensation_events": len(condensation_positions),
        "forgotten_events": sum(
            len(getattr(event, "forgotten_event_ids", set()))
            for event in events
            if type(event).__name__ == "Condensation"
        ),
        "actions": len(action_hashes),
        "unique_actions": len(set(action_hashes)),
        "repeated_actions": len(action_hashes) - len(set(action_hashes)),
        "post_condensation_actions": len(post_condensation),
        "post_condensation_unique_actions": len(set(post_condensation)),
        "final_diff_present": bool(diff.strip()),
        "final_diff_hash": stable_hash(diff) if diff.strip() else None,
        "changed_files": changed_files,
        "meaningful_progress": bool(diff.strip()),
    }


def _controller_gateway_host(configured: Any) -> str | None:
    if configured not in {"auto", "tailscale"}:
        return str(configured) if configured else None
    proc = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", "tailscale0"],
        text=True,
        capture_output=True,
        check=False,
    )
    match = re.search(r"\binet\s+(100\.\d+\.\d+\.\d+)/", proc.stdout)
    return match.group(1) if match else None


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
        elif re.search(
            r"ServiceUnavailable|provider credential pool unavailable|no available credential",
            message,
            re.IGNORECASE,
        ):
            status = 503
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    source_headers = (
        getattr(response, "headers", None) or getattr(exc, "headers", None) or {}
    )
    headers = {
        str(key).lower(): str(value)
        for key, value in dict(source_headers).items()
        if str(key).lower().startswith("x-ratelimit-")
        or str(key).lower() in {"retry-after", "request-id", "x-request-id"}
    }
    quota_exhausted = bool(
        re.search(
            r"(?:quota.*exhaust|exhaust.*quota|quota exceeded)", message, re.IGNORECASE
        )
    )
    if status == 429 or quota_exhausted:
        if quota_exhausted and "retry-after" not in headers:
            # Some ACP agents wrap a provider's daily-quota response as HTTP
            # 500 and omit reset metadata. Avoid immediately selecting the
            # same credential again while retaining a finite re-canary path.
            headers["retry-after"] = "3600"
        return Outcome.RATE_LIMIT, 429, headers
    if re.search(
        r"request context.*sustainable request limit|ContextCapacityExceeded",
        message,
        re.IGNORECASE,
    ):
        return Outcome.RESOURCE_FAILURE, status, headers
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
            raise RuntimeError(
                "OpenHands ACP candidate requires acp_command as a string list"
            )
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

    @staticmethod
    def _accounting_reply(response: Any) -> ChatReply:
        """Convert an OpenHands/LiteLLM response into PMC's ledger contract."""
        raw = getattr(response, "raw_response", None)
        try:
            data = dict(raw or {})
        except (TypeError, ValueError):
            data = {}
        usage = data.get("usage") or {}
        try:
            usage = dict(usage)
        except (TypeError, ValueError):
            usage = {}
        hidden = getattr(raw, "_hidden_params", None) or {}
        headers = hidden.get("additional_headers") or hidden.get("headers") or {}
        rate_headers = {
            str(key).lower(): str(value)
            for key, value in dict(headers).items()
            if str(key).lower().startswith("x-ratelimit-")
            or str(key).lower() in {"retry-after", "request-id", "x-request-id"}
        }
        raw_cost = hidden.get("response_cost") or usage.get("cost") or data.get("cost")
        try:
            cost = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            cost = None
        return ChatReply(
            content="",
            input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
            output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
            request_id=(
                headers.get("x-request-id")
                or headers.get("request-id")
                or data.get("id")
            ),
            cost_usd=cost,
            rate_headers=rate_headers,
            raw=data,
        )

    def _pooled_llm(
        self,
        llm_type: Any,
        secret_type: Any,
        kwargs: dict[str, Any],
        accounting: Any,
        max_failovers: int,
    ) -> Any:
        """Build an LLM whose every provider call uses PMC ProviderPool.

        OpenHands runs the agent/LLM loop in the controller and delegates only
        workspace operations to Agent Server.  This wrapper therefore keeps
        all provider credentials out of the remote worker while allowing each
        model request to reserve and reconcile a different legitimate lane.
        """
        executor = self

        class PooledLLM(llm_type):
            _pmc_accounting: Any = PrivateAttr()
            _pmc_turn: int = PrivateAttr(default=0)
            _pmc_max_failovers: int = PrivateAttr(default=0)

            def completion(self, messages, tools=None, **call_kwargs):
                formatted = []
                for message in messages:
                    if hasattr(message, "model_dump"):
                        formatted.append(message.model_dump(mode="json"))
                    elif isinstance(message, dict):
                        formatted.append(message)
                    else:
                        formatted.append({"content": str(message)})
                max_output = int(
                    call_kwargs.get("max_tokens")
                    or call_kwargs.get("max_output_tokens")
                    or self.max_output_tokens
                    or 4096
                )
                last_error: Exception | None = None
                for _ in range(self._pmc_max_failovers + 1):
                    self._pmc_turn += 1
                    formatted_tools = []
                    for tool in tools or []:
                        if hasattr(tool, "model_dump"):
                            formatted_tools.append(tool.model_dump(mode="json"))
                        elif isinstance(tool, dict):
                            formatted_tools.append(tool)
                        else:
                            formatted_tools.append({"tool": str(tool)})
                    try:
                        ticket = self._pmc_accounting.reserve(
                            self._pmc_turn,
                            formatted,
                            max_output,
                            tools=formatted_tools,
                        )
                    except TypeError as exc:
                        if "unexpected keyword argument 'tools'" not in str(exc):
                            raise
                        ticket = self._pmc_accounting.reserve(
                            self._pmc_turn, formatted, max_output
                        )
                    key = os.getenv(ticket.api_key_env or "")
                    if not key:
                        error = RuntimeError(
                            f"credential environment variable is unavailable: {ticket.api_key_env}"
                        )
                        self._pmc_accounting.fail(ticket, error)
                        raise error
                    # Disable SDK-hidden retries: every physical provider call
                    # must have its own PMC ledger row and reservation.
                    clone = self.model_copy(
                        update={"api_key": secret_type(key), "num_retries": 0}
                    )
                    try:
                        response = llm_type.completion(
                            clone, messages, tools=tools, **call_kwargs
                        )
                    except Exception as exc:
                        self._pmc_accounting.fail(ticket, exc)
                        last_error = exc
                        _outcome, status, _headers = _provider_failure(exc)
                        if status not in {401, 403, 429, 500, 502, 503, 504}:
                            raise
                        continue
                    self._pmc_accounting.succeed(
                        ticket, executor._accounting_reply(response)
                    )
                    return response
                assert last_error is not None
                raise last_error

        pooled = PooledLLM(**{**kwargs, "num_retries": 0, "stream": False})
        pooled._pmc_accounting = accounting
        pooled._pmc_max_failovers = max(0, max_failovers)
        return pooled

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
        kwargs: dict[str, Any] = {
            "model": llm_model,
            "usage_id": f"pmc:{request.job.id}",
        }
        if c.extra.get("max_output_tokens"):
            kwargs["max_output_tokens"] = int(c.extra["max_output_tokens"])
        if "native_tool_calling" in c.extra:
            kwargs["native_tool_calling"] = bool(c.extra["native_tool_calling"])
        gateway = None
        if request.accounting is not None and c.server_url:
            from ..provider_gateway import ProviderGateway

            gateway_host = _controller_gateway_host(
                c.extra.get("controller_gateway_host")
            )
            if not gateway_host:
                return ExecutionResult(
                    False,
                    error=(
                        "remote request-accounted OpenHands candidate requires "
                        "controller_gateway_host (normally the controller's Tailscale IP)"
                    ),
                    outcome=Outcome.POLICY_FAILURE,
                )
            try:
                gateway = ProviderGateway(
                    bind_host=str(gateway_host),
                    public_host=str(gateway_host),
                    upstream_base_url=c.base_url or "",
                    upstream_model=c.model,
                    accounting=request.accounting,
                    max_failovers=int(c.extra.get("provider_request_failovers", 2)),
                    max_rate_limit_wait=float(
                        c.extra.get("provider_rate_limit_max_wait", 30)
                    ),
                    timeout=float(c.extra.get("provider_timeout", 300)),
                ).start()
            except OSError as exc:
                return ExecutionResult(
                    False,
                    error=(
                        "controller provider gateway is unavailable at "
                        f"{gateway_host}: {exc}"
                    ),
                    outcome=Outcome.RESOURCE_FAILURE,
                    accounting_level="per_model_request",
                )
            kwargs["api_key"] = SecretStr(gateway.token)
            kwargs["base_url"] = gateway.base_url
            kwargs["num_retries"] = 0
            kwargs["stream"] = False
        elif key and request.accounting is None:
            kwargs["api_key"] = SecretStr(key)
        if c.base_url and gateway is None:
            kwargs["base_url"] = c.base_url
        if c.extra.get("agent_kind") == "acp":
            agent = self._acp_agent(c)
            llm = agent.llm
            secret_name = c.extra.get("acp_api_key_env")
            conversation_secrets = {secret_name: key} if key and secret_name else None
        else:
            if request.accounting is not None and gateway is None:
                llm = self._pooled_llm(
                    LLM,
                    SecretStr,
                    kwargs,
                    request.accounting,
                    int(c.extra.get("provider_request_failovers", 2)),
                )
            else:
                llm = LLM(**kwargs)
            agent = get_default_agent(llm=llm, cli_mode=True)
            # Make production context behavior explicit and versionable. The SDK
            # default is an LLM summarizer, but custom provider models often have
            # no discoverable input limit, leaving only the late event-count trigger.
            from openhands.sdk.context.condenser import LLMSummarizingCondenser

            if hasattr(agent, "condenser") and hasattr(llm, "model_copy"):
                default_condenser = agent.condenser
                condenser = LLMSummarizingCondenser(
                    llm=llm.model_copy(update={"usage_id": "condenser"}),
                    max_size=int(
                        c.extra.get(
                            "condenser_max_events",
                            getattr(default_condenser, "max_size", 80),
                        )
                    ),
                    max_tokens=(
                        int(c.extra["condenser_max_tokens"])
                        if c.extra.get("condenser_max_tokens")
                        else getattr(default_condenser, "max_tokens", None)
                    ),
                    keep_first=int(
                        c.extra.get(
                            "condenser_keep_first",
                            getattr(default_condenser, "keep_first", 4),
                        )
                    ),
                )
                agent = agent.model_copy(update={"condenser": condenser})
            conversation_secrets = None
        active_condenser = getattr(agent, "condenser", None)
        agent_diagnostics: dict[str, Any] = {
            "condenser": {
                "class": type(active_condenser).__name__,
                "enabled": active_condenser is not None
                and type(active_condenser).__name__ != "NoOpCondenser",
                "max_size": getattr(active_condenser, "max_size", None),
                "max_tokens": getattr(active_condenser, "max_tokens", None),
                "keep_first": getattr(active_condenser, "keep_first", None),
                "llm": "same-candidate",
            }
        }
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
                    os.getenv(c.server_api_key_env) if c.server_api_key_env else None
                )
                remote_dir = f"/tmp/pmc-{request.job.id.lower()}-{request.attempt_no}"
                # The remote agent's tool boundary must be the same directory
                # that receives the repository snapshot. Previously Workspace
                # defaulted to ``workspace/project`` while PMC uploaded under
                # /tmp, leaving every agent tool pointed at an empty directory.
                workspace = Workspace(
                    host=c.server_url,
                    working_dir=remote_dir,
                    api_key=server_key,
                )
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
                    run_error: Exception | None = None
                    diff = ""
                    try:
                        conversation.send_message(remote_prompt)
                        conversation.run()
                    except Exception as exc:  # preserve partial work before classifying
                        run_error = exc
                    finally:
                        try:
                            diff = workspace.execute_command(
                                f"cd {remote_dir} && git add -A && git diff --binary --cached HEAD --"
                            ).stdout
                        except Exception:
                            diff = ""
                        try:
                            events = list(conversation.state.events)
                            agent_diagnostics = _agent_progress_diagnostics(
                                events,
                                diff,
                                {
                                    **agent_diagnostics,
                                    "conversation_id": str(conversation.id),
                                    "execution_status": str(
                                        conversation.state.execution_status
                                    ),
                                    "terminated_with_error": run_error is not None,
                                },
                            )
                        except Exception:
                            # Diagnostics must never hide the original executor
                            # outcome or turn completed coding work into failure.
                            agent_diagnostics = {
                                **agent_diagnostics,
                                "diagnostics": "unavailable",
                                "terminated_with_error": run_error is not None,
                            }
                        conversation.close()
                    if diff.strip():
                        from ..gitops import WorktreeManager

                        WorktreeManager(request.worktree.parent).apply_patch(
                            request.worktree, diff
                        )
                    if run_error is not None:
                        raise run_error
            if request.accounting is not None:
                totals = request.accounting.db.model_request_totals(
                    request.accounting.attempt_id, c.name
                )
                in_tok = int(totals["input_tokens"])
                out_tok = int(totals["output_tokens"])
                cost = totals["cost_usd"]
                raw = {
                    "model_request_totals": totals,
                    "agent": agent_diagnostics,
                }
            else:
                in_tok, out_tok, cost, sdk_raw = self._metrics(llm)
                raw = {**sdk_raw, "agent": agent_diagnostics}
            return ExecutionResult(
                True,
                summary="OpenHands conversation completed; controller will verify independently",
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_usd=cost,
                raw_metrics={
                    **raw,
                    "accounting": (
                        "per_model_request" if request.accounting else "sdk-aggregate"
                    ),
                },
                accounting_level=(
                    "per_model_request" if request.accounting else "aggregate"
                ),
            )
        except Exception as exc:
            if request.accounting is not None:
                totals = request.accounting.db.model_request_totals(
                    request.accounting.attempt_id, c.name
                )
                in_tok = int(totals["input_tokens"])
                out_tok = int(totals["output_tokens"])
                cost = totals["cost_usd"]
                raw = {
                    "model_request_totals": totals,
                    "agent": agent_diagnostics,
                }
            else:
                in_tok, out_tok, cost, sdk_raw = self._metrics(llm)
                raw = {**sdk_raw, "agent": agent_diagnostics}
            outcome, provider_status, rate_headers = _provider_failure(exc)
            if outcome == Outcome.RESOURCE_FAILURE and re.search(
                r"request context.*sustainable request limit|ContextCapacityExceeded",
                str(exc),
                re.IGNORECASE,
            ):
                raw["context_incompatible"] = True
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
                raw_metrics={
                    **raw,
                    "accounting": (
                        "per_model_request" if request.accounting else "sdk-aggregate"
                    ),
                },
                outcome=outcome,
                accounting_level=(
                    "per_model_request" if request.accounting else "aggregate"
                ),
            )
        finally:
            if gateway is not None:
                gateway.close()
