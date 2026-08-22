from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(slots=True)
class ChatReply:
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    request_id: str | None = None
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
        waits = [0, 2, 5, 10, 20]
        last_response: httpx.Response | None = None
        with httpx.Client(timeout=self.timeout) as client:
            for i, wait in enumerate(waits):
                if wait:
                    time.sleep(wait)
                response = client.post(url, headers=headers, json=payload)
                last_response = response
                if response.status_code < 400:
                    break
                transient = response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
                if not transient or i == len(waits) - 1:
                    response.raise_for_status()
                retry_after = response.headers.get("retry-after")
                if retry_after:
                    try:
                        waits[min(i + 1, len(waits) - 1)] = min(60, float(retry_after))
                    except ValueError:
                        pass
            assert last_response is not None
            response = last_response
            response.raise_for_status()
            data = response.json()
        choice = data["choices"][0]
        content = choice.get("message", {}).get("content") or ""
        usage = data.get("usage") or {}
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
            rate_headers=rate_headers,
            raw=data,
        )
