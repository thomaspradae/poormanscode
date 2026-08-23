from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .context import build_context_bundle
from .domain import Candidate
from .providers import OpenAICompatibleClient


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("plan.tasks must be a non-empty array")
    keys: list[str] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise TypeError(f"task {index} must be an object")
        key = task.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", key):
            raise ValueError(f"task {index} has invalid id")
        if key in keys:
            raise ValueError(f"duplicate task id: {key}")
        if not isinstance(task.get("request"), str) or not task["request"].strip():
            raise ValueError(f"task {key} requires request")
        acceptance = task.get("acceptance", [])
        depends = task.get("depends_on", [])
        candidate_order = task.get("candidate_order", [])
        if not isinstance(acceptance, list) or not all(
            isinstance(x, str) for x in acceptance
        ):
            raise ValueError(f"task {key} acceptance must be strings")
        if not isinstance(depends, list) or not all(
            isinstance(x, str) for x in depends
        ):
            raise ValueError(f"task {key} depends_on must be strings")
        if not isinstance(candidate_order, list) or not all(
            isinstance(x, str) for x in candidate_order
        ):
            raise ValueError(f"task {key} candidate_order must be strings")
        keys.append(key)
    key_set = set(keys)
    graph = {task["id"]: list(task.get("depends_on", [])) for task in tasks}
    for key, deps in graph.items():
        missing = set(deps) - key_set
        if missing:
            raise ValueError(f"task {key} has unknown dependencies: {sorted(missing)}")
        if key in deps:
            raise ValueError(f"task {key} depends on itself")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError("dependency graph contains a cycle")
        if key in visited:
            return
        visiting.add(key)
        for dep in graph[key]:
            visit(dep)
        visiting.remove(key)
        visited.add(key)

    for key in keys:
        visit(key)
    referenced = {dep for deps in graph.values() for dep in deps}
    sinks = [key for key in keys if key not in referenced]
    if len(sinks) != 1:
        raise ValueError(
            f"plan must have exactly one terminal integration task; found {sinks}"
        )
    return {"version": 1, "tasks": tasks}


def _json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def propose_plan(
    repo: Path, title: str, request: str, candidate: Candidate
) -> dict[str, Any]:
    if not candidate.model or not candidate.base_url:
        raise ValueError("Foreman candidate must expose an OpenAI-compatible model")
    context = build_context_bundle(repo, request).content
    prompt = f"""Decompose this software feature into the smallest useful dependency DAG.
Every task must be independently implementable and verifiable. Avoid needless decomposition.
Return JSON only: {{"tasks":[{{"id":"short-id","request":"...","acceptance":["..."],"depends_on":[],"task_type":"FEATURE","priority":2,"candidate_order":["groq-oss20-bash","jules","local-qwen25-coder-7b-bash"]}}]}}.
Cover every part of the parent request. Bootstrap/setup must precede tasks that require it.

FEATURE: {title}
REQUEST: {request}

CONTEXT:
{context}
"""
    reply = OpenAICompatibleClient(candidate.base_url, candidate.api_key_env).chat(
        model=candidate.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=int(candidate.extra.get("foreman_max_tokens", 4096)),
        extra_body=candidate.extra.get("request_extra"),
    )
    return validate_plan(_json_object(reply.content))
