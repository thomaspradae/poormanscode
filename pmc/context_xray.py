from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .versioning import stable_hash


def estimated_tokens(value: Any) -> int:
    """Conservative transport-level estimate without retaining prompt content."""
    try:
        chars = len(json.dumps(value, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        chars = len(str(value))
    return max(1, (chars + 3) // 4)


def _role(message: dict[str, Any]) -> str:
    return str(message.get("role") or "unknown").lower()


def _is_tool_observation(message: dict[str, Any]) -> bool:
    if _role(message) == "tool" or message.get("tool_call_id"):
        return True
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    return any(
        isinstance(block, dict)
        and str(block.get("type", "")).lower()
        in {"tool_result", "function_response", "tool_response"}
        for block in blocks
    )


def analyze_request_context(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    model_context_window: int | None = None,
    previous_estimated_input: int | None = None,
    previous_request_hash: str | None = None,
    condensation_count: int = 0,
) -> dict[str, Any]:
    """Return a content-free token X-ray for one physical model request."""
    tools = tools or []
    total = estimated_tokens({"messages": messages, "tools": tools})
    first_user = next(
        (index for index, message in enumerate(messages) if _role(message) == "user"),
        None,
    )
    composition = Counter()
    hashes: list[str] = []
    for index, message in enumerate(messages):
        hashes.append(stable_hash(message))
        tokens = estimated_tokens(message)
        role = _role(message)
        if role in {"system", "developer"}:
            bucket = "system_prompt"
        elif _is_tool_observation(message):
            bucket = "tool_observations"
        elif first_user is not None and index == first_user:
            bucket = "original_task"
        elif role == "assistant":
            bucket = "model_messages"
        else:
            bucket = "other_history"
        composition[bucket] += tokens
    composition["tool_schemas"] = estimated_tokens(tools) if tools else 0
    accounted = sum(composition.values())
    composition["serialization_overhead"] = max(0, total - accounted)
    request_hash = stable_hash({"messages": messages, "tools": tools})
    duplicate_messages = sum(count - 1 for count in Counter(hashes).values() if count > 1)
    request_kind = (
        "condenser"
        if not tools and len(messages) == 1 and _role(messages[0]) == "user"
        else "agent"
    )
    occupancy = (
        round(total / model_context_window, 6)
        if model_context_window and model_context_window > 0
        else None
    )
    return {
        "request_kind": request_kind,
        "request_hash": request_hash,
        "estimated_input_tokens": total,
        "model_context_window": model_context_window,
        "context_occupancy": occupancy,
        "message_count": len(messages),
        "tool_count": len(tools),
        "composition": dict(composition),
        "condensation_count_before": condensation_count,
        "growth_tokens": (
            total - previous_estimated_input
            if previous_estimated_input is not None
            else None
        ),
        "unchanged_from_previous": request_hash == previous_request_hash,
        "duplicate_messages_within_request": duplicate_messages,
    }
