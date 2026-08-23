from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx


class ProviderError(RuntimeError):
    def __init__(self, status_code: int, message: str, rate_headers: dict[str, str]):
        super().__init__(message)
        self.status_code = status_code
        self.rate_headers = rate_headers


@dataclass(slots=True)
class ChatReply:
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_id: str | None = None
    cost_usd: float | None = None
    rate_headers: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key_env: str | None,
        timeout: float = 300,
        extra_headers: dict[str, str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env or ""
        self.timeout = timeout
        self.extra_headers = extra_headers or {}

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        extra_body: dict[str, Any] | None = None,
    ) -> ChatReply:
        key = os.getenv(self.api_key_env) if self.api_key_env else None
        headers = {"Accept": "application/json", **self.extra_headers}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if extra_body:
            payload.update(extra_body)
        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=self.timeout) as client:
            # Retries are deliberately controller-visible. A hidden HTTP retry
            # would consume quota without its own model-request ledger record.
            response = client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                rate = {
                    k.lower(): v
                    for k, v in response.headers.items()
                    if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"
                }
                raise ProviderError(
                    response.status_code, f"provider HTTP {response.status_code}", rate
                )
            data = response.json()
        choice = data["choices"][0]
        content = choice.get("message", {}).get("content") or ""
        usage = data.get("usage") or {}
        raw_cost = usage.get("cost") or data.get("cost")
        try:
            cost = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            cost = None
        rate_headers = {
            k.lower(): v
            for k, v in response.headers.items()
            if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"
        }
        return ChatReply(
            content=content,
            input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
            output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
            request_id=response.headers.get("x-request-id") or data.get("id"),
            cost_usd=cost,
            rate_headers=rate_headers,
            raw=data,
        )
