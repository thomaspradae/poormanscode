from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from typing import Any

import httpx

from ..accounting import BudgetExceeded
from ..domain import ExecutionRequest, ExecutionResult, Outcome
from ..providers import OpenAICompatibleClient, ProviderError
from ..sandbox import SandboxLimits, build_sandbox, scrubbed_environment

SYSTEM = """You are a software engineering agent. Use the provided shell tool to inspect and modify the repository.
Use the research tool when current authoritative documentation is required; it is controller-mediated and returns sourced results.
When the ticket is fully implemented, respond with exactly one JSON object as ordinary text and no markdown:
{"action":"done","summary":"..."}
Prefer small, targeted changes. Never commit or push.
Do not claim success until you have run the visible relevant tests yourself.
"""

SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Run a bash command inside the isolated repository worktree.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}
RESEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "research",
        "description": "Ask the controller to research current authoritative information using Google Search grounding.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}
DONE_TOOL = {
    "type": "function",
    "function": {
        "name": "JSON",
        "description": "Report that the ticket is complete after all required shell work and verification finished.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["done"]},
                "summary": {"type": "string"},
            },
            "required": ["action", "summary"],
            "additionalProperties": False,
        },
    },
}


class UnsafeCommand(RuntimeError):
    pass


def _clean_child_env() -> dict[str, str]:
    return scrubbed_environment()


def _guard(command: str) -> None:
    forbidden = [
        r"(^|\s)sudo(\s|$)",
        r"(^|\s)(ssh|scp)(\s|$)",
        r"git\s+push\b",
        r"git\s+commit\b",
        r"git\s+reset\s+--hard\b",
        r"rm\s+-[^\n]*r[^\n]*f[^\n]*\s+/(?:\s|$)",
        r"/etc/",
        r"/root/",
        r"\.\./\.\./\.\./",
    ]
    for pattern in forbidden:
        if re.search(pattern, command):
            raise UnsafeCommand(f"blocked shell command by policy: {command[:300]}")


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


class BashExecutor:
    name = "bash"

    def _command(
        self, request: ExecutionRequest, command: str
    ) -> subprocess.CompletedProcess[str]:
        _guard(command)
        c = request.candidate
        env = _clean_child_env()
        limits = SandboxLimits(
            wall_seconds=int(c.extra.get("command_timeout", 180)),
            cpu_seconds=int(c.extra.get("cpu_seconds", 120)),
            memory_bytes=int(c.extra.get("memory_mb", 2048)) * 1024**2,
            processes=int(c.extra.get("process_limit", 128)),
            file_bytes=int(c.extra.get("file_mb", 512)) * 1024**2,
            workspace_bytes=int(c.extra.get("workspace_mb", 2048)) * 1024**2,
            workspace_files=int(c.extra.get("workspace_files", 50_000)),
            artifact_bytes=int(c.extra.get("artifact_mb", 512)) * 1024**2,
        )
        sandbox = build_sandbox(c.sandbox)
        policy = c.effective_network_policy
        if not sandbox.supports_network_policy(policy):
            raise RuntimeError(
                f"sandbox {sandbox.name} cannot enforce network_policy={policy}"
            )
        return sandbox.run(
            request.worktree, command, env=env, network=policy == "full", limits=limits
        )

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        c = request.candidate
        if not c.model or not c.base_url:
            return ExecutionResult(
                False, error="bash executor requires model and base_url"
            )
        client = OpenAICompatibleClient(c.base_url, c.api_key_env)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": request.prompt},
        ]
        in_tokens = 0
        out_tokens = 0
        cost_usd = 0.0
        request_id = None
        rate_headers: dict[str, str] = {}
        transcript: list[dict[str, Any]] = []
        protocol_errors = 0
        format_errors = 0
        rate_retries = int(c.extra.get("rate_limit_retries", 3))
        server_retries = int(c.extra.get("server_error_retries", 2))
        tool_protocol_retries = int(c.extra.get("tool_protocol_retries", 2))
        command_counts: dict[str, int] = {}
        no_progress_turns = 0
        last_diff = ""
        for turn in range(c.max_turns):
            rate_try = 0
            server_try = 0
            tool_protocol_try = 0
            while True:
                ticket = None
                try:
                    if request.accounting:
                        ticket = request.accounting.reserve(
                            turn + 1, messages, int(c.extra.get("max_tokens", 4096))
                        )
                        client.api_key_env = ticket.api_key_env
                    reply = client.chat(
                        model=c.model,
                        messages=messages,
                        temperature=float(c.extra.get("temperature", 0.0)),
                        max_tokens=int(c.extra.get("max_tokens", 4096)),
                        extra_body={
                            "parallel_tool_calls": False,
                            **dict(c.extra.get("request_extra") or {}),
                        },
                        tools=[SHELL_TOOL, DONE_TOOL]
                        + ([RESEARCH_TOOL] if request.research else []),
                    )
                    if ticket:
                        request.accounting.succeed(ticket, reply)
                    break
                except BudgetExceeded as exc:
                    return ExecutionResult(
                        False, error=str(exc), outcome=Outcome.POLICY_FAILURE,
                        input_tokens=in_tokens, output_tokens=out_tokens,
                        cost_usd=cost_usd,
                    )
                except ProviderError as exc:
                    if ticket:
                        request.accounting.fail(ticket, exc)
                    retry_after = exc.rate_headers.get("retry-after")
                    if exc.status_code == 429 and rate_try < rate_retries:
                        rate_try += 1
                        try:
                            delay = max(1.0, min(float(retry_after or 1), 120.0))
                        except ValueError:
                            delay = 1.0
                        transcript.append(
                            {"turn": turn + 1, "rate_limited": True, "wait": delay}
                        )
                        time.sleep(delay)
                        continue
                    if 500 <= exc.status_code < 600 and server_try < server_retries:
                        server_try += 1
                        delay = float(min(2 ** (server_try - 1), 8))
                        transcript.append(
                            {
                                "turn": turn + 1,
                                "provider_server_error": exc.status_code,
                                "retry": server_try,
                                "wait": delay,
                            }
                        )
                        time.sleep(delay)
                        continue
                    if (
                        exc.status_code == 400
                        and exc.error_code == "tool_use_failed"
                        and tool_protocol_try < tool_protocol_retries
                    ):
                        tool_protocol_try += 1
                        transcript.append(
                            {
                                "turn": turn + 1,
                                "tool_protocol_retry": tool_protocol_try,
                                "reason": exc.failed_generation.get("reason"),
                            }
                        )
                        continue
                    return ExecutionResult(
                        False,
                        error=str(exc),
                        input_tokens=in_tokens or None,
                        output_tokens=out_tokens or None,
                        cost_usd=cost_usd or None,
                        raw_metrics={
                            "turns": transcript,
                            "provider_status": exc.status_code,
                            "rate_headers": exc.rate_headers,
                        },
                        outcome=(
                            Outcome.RATE_LIMIT
                            if exc.status_code == 429
                            else (
                                Outcome.PROTOCOL_FAILURE
                                if exc.error_code == "tool_use_failed"
                                else Outcome.PROVIDER_FAILURE
                            )
                        ),
                    )
                except httpx.TimeoutException as exc:
                    if ticket:
                        request.accounting.fail(ticket, exc)
                    return ExecutionResult(
                        False,
                        error=f"model request timed out: {exc}",
                        input_tokens=in_tokens or None,
                        output_tokens=out_tokens or None,
                        cost_usd=cost_usd or None,
                        raw_metrics={"turns": transcript, "timeout_source": "model"},
                        outcome=Outcome.TIMEOUT,
                    )
                except Exception as exc:  # noqa: BLE001 - normalize client failures
                    if ticket:
                        request.accounting.fail(ticket, exc)
                    return ExecutionResult(
                        False,
                        error=f"LLM request failed: {type(exc).__name__}: {exc}",
                        input_tokens=in_tokens or None,
                        output_tokens=out_tokens or None,
                        cost_usd=cost_usd or None,
                        raw_metrics={"turns": transcript},
                        outcome=Outcome.PROVIDER_FAILURE,
                    )
            in_tokens += reply.input_tokens or 0
            out_tokens += reply.output_tokens or 0
            cost_usd += reply.cost_usd or 0
            request_id = reply.request_id or request_id
            rate_headers = reply.rate_headers or rate_headers
            if reply.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply.content or None,
                        "tool_calls": reply.tool_calls,
                    }
                )
                call = reply.tool_calls[0]
                try:
                    arguments = json.loads(call["function"]["arguments"])
                    tool_name = call["function"]["name"]
                    if tool_name == "shell":
                        command = arguments.get("command") or arguments.get("cmd")
                        if command is None and len(arguments) == 1:
                            command = next(iter(arguments.values()))
                        if not isinstance(command, str):
                            raise KeyError("command")
                        action = {"action": "bash", "command": command}
                    elif tool_name == "JSON":
                        action = {
                            "action": arguments.get("action"),
                            "summary": arguments.get("summary"),
                        }
                    elif tool_name == "research" and request.research:
                        action = {"action": "research", "query": arguments["query"]}
                    else:
                        raise KeyError(f"unsupported tool {tool_name}")
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    return ExecutionResult(
                        False,
                        error=f"invalid shell tool call: {exc}",
                        outcome=Outcome.PROTOCOL_FAILURE,
                    )
            else:
                messages.append({"role": "assistant", "content": reply.content})
                try:
                    action = _extract_json(reply.content)
                    if action.get("name") == "shell" and "action" not in action:
                        arguments = action.get("arguments", {})
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                        command = arguments.get("command") or arguments.get("cmd")
                        if isinstance(command, str):
                            action = {"action": "bash", "command": command}
                except Exception as exc:  # noqa: BLE001 - report parser failure to model
                    protocol_errors += 1
                    obs = f"Protocol error: use the shell tool or return the done JSON. Parser error: {exc}"
                    messages.append({"role": "user", "content": obs})
                    transcript.append(
                        {"turn": turn + 1, "protocol_error": reply.content[:1000]}
                    )
                    continue
            kind = action.get("action")
            if kind == "done":
                return ExecutionResult(
                    True,
                    summary=str(action.get("summary") or "worker reported completion"),
                    input_tokens=in_tokens or None,
                    output_tokens=out_tokens or None,
                    cost_usd=cost_usd or None,
                    provider_request_id=request_id,
                    raw_metrics={
                        "turns": transcript,
                        "turn_count": turn + 1,
                        "rate_headers": rate_headers,
                    },
                )
            if kind == "research" and isinstance(action.get("query"), str):
                try:
                    researched = request.research.search(action["query"])
                    observation = json.dumps(
                        {"answer": researched.text, "sources": researched.sources}
                    )
                except Exception as exc:  # noqa: BLE001 - research failures are observations
                    observation = f"research error: {type(exc).__name__}: {exc}"
                transcript.append({"turn": turn + 1, "research": action["query"]})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": "research",
                        "content": observation,
                    }
                )
                continue
            if kind != "bash" or not isinstance(action.get("command"), str):
                format_errors += 1
                messages.append(
                    {
                        "role": "user",
                        "content": "Invalid action. Use action=bash or action=done.",
                    }
                )
                continue
            command = action["command"]
            command_key = hashlib.sha256(command.strip().encode()).hexdigest()
            command_counts[command_key] = command_counts.get(command_key, 0) + 1
            if command_counts[command_key] > int(
                c.extra.get("repeat_command_limit", 3)
            ):
                return ExecutionResult(
                    False,
                    error="repeated-command convergence limit exceeded",
                    input_tokens=in_tokens or None,
                    output_tokens=out_tokens or None,
                    cost_usd=cost_usd or None,
                    outcome=Outcome.EXECUTOR_FAILURE,
                    raw_metrics={
                        "turns": transcript,
                        "convergence": "repeated_command",
                    },
                )
            try:
                started = time.monotonic()
                p = self._command(request, command)
                elapsed = time.monotonic() - started
                if "PMC_RESOURCE_LIMIT:TIMEOUT" in p.stderr:
                    return ExecutionResult(
                        False,
                        error="worker command timed out",
                        input_tokens=in_tokens or None,
                        output_tokens=out_tokens or None,
                        cost_usd=cost_usd or None,
                        outcome=Outcome.TIMEOUT,
                        raw_metrics={"turns": transcript, "timeout_source": "shell"},
                    )
                if "PMC_RESOURCE_LIMIT:WORKSPACE_LIMIT" in p.stderr:
                    return ExecutionResult(
                        False,
                        error=p.stderr.strip(),
                        input_tokens=in_tokens or None,
                        output_tokens=out_tokens or None,
                        cost_usd=cost_usd or None,
                        outcome=Outcome.RESOURCE_FAILURE,
                        raw_metrics={"turns": transcript},
                    )
                if "PMC_POLICY_FAILURE:" in p.stderr:
                    return ExecutionResult(
                        False,
                        error=p.stderr.strip(),
                        input_tokens=in_tokens or None,
                        output_tokens=out_tokens or None,
                        cost_usd=cost_usd or None,
                        outcome=Outcome.POLICY_FAILURE,
                        raw_metrics={"turns": transcript},
                    )
                observation = (
                    f"exit={p.returncode} duration={elapsed:.2f}s\n"
                    f"STDOUT:\n{p.stdout[-12000:]}\nSTDERR:\n{p.stderr[-12000:]}"
                )
                transcript.append(
                    {
                        "turn": turn + 1,
                        "command": command,
                        "exit": p.returncode,
                        "seconds": elapsed,
                    }
                )
                diff_now = subprocess.run(
                    ["git", "-C", str(request.worktree), "diff", "--binary"],
                    text=True,
                    capture_output=True,
                    timeout=20,
                    check=False,
                ).stdout
                if diff_now == last_diff:
                    no_progress_turns += 1
                else:
                    no_progress_turns = 0
                    last_diff = diff_now
                no_diff_limit = int(c.extra.get("no_diff_limit", 12))
                token_limit = int(c.extra.get("tokens_without_progress", 20_000))
                if (
                    no_progress_turns >= no_diff_limit
                    and (in_tokens + out_tokens) >= token_limit
                ):
                    return ExecutionResult(
                        False,
                        error="token-without-progress convergence limit exceeded",
                        input_tokens=in_tokens or None,
                        output_tokens=out_tokens or None,
                        cost_usd=cost_usd or None,
                        outcome=Outcome.EXECUTOR_FAILURE,
                        raw_metrics={"turns": transcript, "convergence": "no_diff"},
                    )
                if no_progress_turns == int(c.extra.get("no_diff_warning_turns", 6)):
                    observation += "\nPMC WARNING: no repository change has been observed; make concrete progress now."
            except UnsafeCommand as exc:
                return ExecutionResult(
                    False,
                    error=str(exc),
                    input_tokens=in_tokens or None,
                    output_tokens=out_tokens or None,
                    cost_usd=cost_usd or None,
                    outcome=Outcome.SECURITY_FAILURE,
                    raw_metrics={"turns": transcript},
                )
            except Exception as exc:  # noqa: BLE001 - tool errors become observations
                observation = f"tool error: {type(exc).__name__}: {exc}"
                transcript.append(
                    {"turn": turn + 1, "command": command, "tool_error": str(exc)}
                )
            if reply.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": "shell",
                        "content": observation,
                    }
                )
            else:
                messages.append({"role": "user", "content": observation})
        return ExecutionResult(
            False,
            error=f"agent exceeded max_turns={c.max_turns}",
            input_tokens=in_tokens or None,
            output_tokens=out_tokens or None,
            cost_usd=cost_usd or None,
            provider_request_id=request_id,
            raw_metrics={
                "turns": transcript,
                "turn_count": c.max_turns,
                "rate_headers": rate_headers,
            },
            outcome=(
                Outcome.PROTOCOL_FAILURE
                if protocol_errors
                else (
                    Outcome.FORMAT_FAILURE
                    if format_errors
                    else Outcome.EXECUTOR_FAILURE
                )
            ),
        )
