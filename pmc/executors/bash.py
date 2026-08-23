from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from ..domain import ExecutionRequest, ExecutionResult
from ..providers import OpenAICompatibleClient, ProviderError
from ..sandbox import SandboxLimits, build_sandbox, scrubbed_environment


SYSTEM = """You are a software engineering agent with one tool: shell.
Respond with exactly one JSON object and no markdown.
To run a command: {"action":"bash","command":"..."}
When the ticket is fully implemented: {"action":"done","summary":"..."}
Use shell commands to inspect and edit files. Prefer small, targeted changes. Never commit or push.
Do not claim success until you have run the visible relevant tests yourself.
"""


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

    def _command(self, request: ExecutionRequest, command: str) -> subprocess.CompletedProcess[str]:
        _guard(command)
        c = request.candidate
        env = _clean_child_env()
        limits = SandboxLimits(
            wall_seconds=int(c.extra.get("command_timeout", 180)),
            cpu_seconds=int(c.extra.get("cpu_seconds", 120)),
            memory_bytes=int(c.extra.get("memory_mb", 2048)) * 1024**2,
            processes=int(c.extra.get("process_limit", 128)),
            file_bytes=int(c.extra.get("file_mb", 512)) * 1024**2,
        )
        return build_sandbox(c.sandbox).run(request.worktree, command, env=env,
                                            network=c.network, limits=limits)

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        c = request.candidate
        if not c.model or not c.base_url:
            return ExecutionResult(False, error="bash executor requires model and base_url")
        client = OpenAICompatibleClient(c.base_url, c.api_key_env)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": request.prompt},
        ]
        in_tokens = 0
        out_tokens = 0
        request_id = None
        rate_headers: dict[str, str] = {}
        transcript: list[dict[str, Any]] = []
        for turn in range(c.max_turns):
            try:
                reply = client.chat(
                    model=c.model,
                    messages=messages,
                    temperature=float(c.extra.get("temperature", 0.0)),
                    max_tokens=int(c.extra.get("max_tokens", 4096)),
                    extra_body=c.extra.get("request_extra"),
                )
            except ProviderError as exc:
                return ExecutionResult(
                    False, error=str(exc), input_tokens=in_tokens or None,
                    output_tokens=out_tokens or None,
                    raw_metrics={"turns": transcript, "provider_status": exc.status_code,
                                 "rate_headers": exc.rate_headers},
                )
            except Exception as exc:
                return ExecutionResult(
                    False,
                    error=f"LLM request failed: {type(exc).__name__}: {exc}",
                    input_tokens=in_tokens or None,
                    output_tokens=out_tokens or None,
                    raw_metrics={"turns": transcript},
                )
            in_tokens += reply.input_tokens or 0
            out_tokens += reply.output_tokens or 0
            request_id = reply.request_id or request_id
            rate_headers = reply.rate_headers or rate_headers
            messages.append({"role": "assistant", "content": reply.content})
            try:
                action = _extract_json(reply.content)
            except Exception as exc:
                obs = f"Protocol error: return exactly one JSON action. Parser error: {exc}"
                messages.append({"role": "user", "content": obs})
                transcript.append({"turn": turn + 1, "protocol_error": reply.content[:1000]})
                continue
            kind = action.get("action")
            if kind == "done":
                return ExecutionResult(
                    True,
                    summary=str(action.get("summary") or "worker reported completion"),
                    input_tokens=in_tokens or None,
                    output_tokens=out_tokens or None,
                    provider_request_id=request_id,
                    raw_metrics={"turns": transcript, "turn_count": turn + 1, "rate_headers": rate_headers},
                )
            if kind != "bash" or not isinstance(action.get("command"), str):
                messages.append({"role": "user", "content": "Invalid action. Use action=bash or action=done."})
                continue
            command = action["command"]
            try:
                started = time.monotonic()
                p = self._command(request, command)
                elapsed = time.monotonic() - started
                observation = (
                    f"exit={p.returncode} duration={elapsed:.2f}s\n"
                    f"STDOUT:\n{p.stdout[-12000:]}\nSTDERR:\n{p.stderr[-12000:]}"
                )
                transcript.append(
                    {"turn": turn + 1, "command": command, "exit": p.returncode, "seconds": elapsed}
                )
            except Exception as exc:
                observation = f"tool error: {type(exc).__name__}: {exc}"
                transcript.append({"turn": turn + 1, "command": command, "tool_error": str(exc)})
            messages.append({"role": "user", "content": observation})
        return ExecutionResult(
            False,
            error=f"agent exceeded max_turns={c.max_turns}",
            input_tokens=in_tokens or None,
            output_tokens=out_tokens or None,
            provider_request_id=request_id,
            raw_metrics={"turns": transcript, "turn_count": c.max_turns, "rate_headers": rate_headers},
        )
