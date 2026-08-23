from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class ResearchResult:
    text: str
    sources: list[dict[str, Any]]
    search_queries: int


class ResearchService:
    """Controller-side Google Search grounding; worker sandboxes retain no network/key."""

    def __init__(
        self,
        db,
        *,
        job_id: str,
        attempt_id: int,
        model: str,
        api_key_env: str,
        max_queries: int,
    ):
        self.db, self.job_id, self.attempt_id = db, job_id, attempt_id
        self.model, self.api_key_env, self.max_queries = model, api_key_env, max_queries
        self.used = 0

    def search(self, query: str) -> ResearchResult:
        if self.used >= self.max_queries:
            raise RuntimeError("research query budget exhausted")
        key = os.getenv(self.api_key_env)
        if not key:
            raise RuntimeError(f"research credential unavailable: {self.api_key_env}")
        self.used += 1
        rid = self.db.begin_research(self.job_id, self.attempt_id, self.model, query)
        self.db.event(
            "RESEARCH_REQUESTED",
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            payload={"research_id": rid, "model": self.model},
        )
        try:
            response = httpx.post(
                "https://generativelanguage.googleapis.com/v1beta/interactions",
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "input": query,
                    "tools": [{"type": "google_search"}],
                },
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()
            text_parts, sources, searches = [], [], 0
            for step in data.get("steps", []):
                kind = step.get("type")
                if kind == "google_search_call":
                    searches += 1
                if kind == "model_output":
                    text_parts.append(step.get("text", ""))
                    for annotation in step.get("annotations", []):
                        if annotation.get("url"):
                            sources.append(
                                {
                                    "url": annotation["url"],
                                    "title": annotation.get("title"),
                                }
                            )
            text = "\n".join(x for x in text_parts if x) or str(data.get("output", ""))
            usage = data.get("usage", {})
            self.db.finish_research(
                rid,
                state="SUCCEEDED",
                text=text,
                sources=sources,
                search_queries=searches,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
            self.db.event(
                "RESEARCH_COMPLETED",
                job_id=self.job_id,
                attempt_id=self.attempt_id,
                payload={
                    "research_id": rid,
                    "search_queries": searches,
                    "sources": sources,
                },
            )
            return ResearchResult(text, sources, searches)
        except Exception as exc:
            self.db.finish_research(
                rid, state="FAILED", error=f"{type(exc).__name__}: {exc}"
            )
            self.db.event(
                "RESEARCH_FAILED",
                job_id=self.job_id,
                attempt_id=self.attempt_id,
                payload={"research_id": rid, "error_type": type(exc).__name__},
            )
            raise
