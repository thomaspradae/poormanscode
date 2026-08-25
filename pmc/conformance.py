from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

import httpx

from .db import Database
from .domain import Candidate
from .executors.bash import SHELL_TOOL, _extract_json
from .providers import OpenAICompatibleClient, ProviderError


def smoke_candidate(db: Database, candidate: Candidate) -> dict[str, Any]:
    """Verify generation and native tool protocol without exposing credentials."""
    key_env = db.provider_credential_env(candidate.provider) if candidate.provider else None
    c = replace(candidate, api_key_env=key_env or candidate.api_key_env)
    details: dict[str, Any] = {"provider": c.provider, "model": c.model}
    generation_ok = tool_ok = False
    try:
        if c.executor == "jules":
            # Jules is an external task executor, so validate its authenticated
            # task/source API rather than pretending it has a native shell tool.
            key = os.getenv(c.api_key_env or "")
            response = httpx.get(
                "https://jules.googleapis.com/v1alpha/sources",
                headers={"x-goog-api-key": key or ""}, timeout=30,
            )
            response.raise_for_status()
            generation_ok = isinstance(response.json().get("sources", []), list)
            tool_ok = generation_ok
            details["kind"] = "external-executor-api"
        elif c.executor == "openhands" and c.base_url and c.model:
            # OpenHands keeps model inference in the PMC controller and uses the
            # remote Agent Server only for workspace/tool execution.  Smoke both
            # halves independently so a provider can be generation-ready while
            # a worker remains quarantined if its workspace endpoint is broken.
            client = OpenAICompatibleClient(c.base_url, c.api_key_env, timeout=90)
            reply = client.chat(
                model=c.model,
                messages=[{"role": "user", "content": "Reply with exactly PMC_SMOKE_OK"}],
                temperature=0,
                max_tokens=256,
            )
            generation_ok = "PMC_SMOKE_OK" in (reply.content or "")
            if c.server_url:
                from openhands.sdk import Workspace

                server_key = (
                    os.getenv(c.server_api_key_env)
                    if c.server_api_key_env
                    else None
                )
                workspace = Workspace(host=c.server_url, api_key=server_key)
                result = workspace.execute_command("printf PMC_OPENHANDS_TOOL_OK")
                tool_ok = "PMC_OPENHANDS_TOOL_OK" in (result.stdout or "")
            details["kind"] = "controller-llm-remote-workspace"
            details["request_ids_present"] = bool(reply.request_id)
        elif c.executor == "bash" and c.base_url and c.model:
            client = OpenAICompatibleClient(c.base_url, c.api_key_env, timeout=90)
            reply = client.chat(
                model=c.model,
                messages=[{"role": "user", "content": "Reply with exactly PMC_SMOKE_OK"}],
                # Reasoning models may consume a small hidden reasoning budget
                # before emitting the visible sentinel.
                temperature=0, max_tokens=256,
            )
            generation_ok = "PMC_SMOKE_OK" in (reply.content or "")
            tool_reply = client.chat(
                model=c.model,
                messages=[{
                    "role": "user",
                    "content": "Use the shell tool exactly once with command: printf PMC_TOOL_OK",
                }],
                temperature=0, max_tokens=256, tools=[SHELL_TOOL],
            )
            tool_ok = any(
                call.get("function", {}).get("name") == "shell"
                and "PMC_TOOL_OK" in str(call.get("function", {}).get("arguments", ""))
                for call in tool_reply.tool_calls
            )
            if not tool_ok and tool_reply.content:
                try:
                    action = _extract_json(tool_reply.content)
                    arguments = action.get("arguments", {})
                    if isinstance(arguments, str):
                        import json
                        arguments = json.loads(arguments)
                    command = arguments.get("command") or arguments.get("cmd")
                    if action.get("action") == "bash":
                        command = action.get("command")
                    tool_ok = action.get("name") == "shell" and "PMC_TOOL_OK" in str(command)
                    tool_ok |= action.get("action") == "bash" and "PMC_TOOL_OK" in str(command)
                except (ValueError, TypeError, AttributeError):
                    pass
            details["request_ids_present"] = bool(reply.request_id or tool_reply.request_id)
        else:
            details["error"] = "unsupported executor for conformance smoke"
    except Exception as exc:  # noqa: BLE001 - smoke must quarantine any adapter failure
        details["error"] = f"{type(exc).__name__}: {exc}"
        transient = isinstance(exc, ProviderError) and (
            exc.status_code == 429 or exc.status_code >= 500
        )
    else:
        transient = False
    db.set_model_conformance(
        candidate, generation_ok=generation_ok, tool_ok=tool_ok, details=details,
        status=("DEGRADED" if transient else None),
    )
    row = db.model_conformance(candidate)
    return {"candidate": candidate.name, "generation": generation_ok, "tool": tool_ok,
            "status": row["status"]}
