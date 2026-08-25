from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Self

import httpx

from .providers.openai_compat import ChatReply, ProviderError

_RATE_HEADERS = {
    "retry-after",
    "request-id",
    "x-request-id",
}


def _rate_headers(headers: Any) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in dict(headers).items()
        if str(key).lower().startswith("x-ratelimit-")
        or str(key).lower() in _RATE_HEADERS
    }


class ProviderGateway:
    """Ephemeral authenticated OpenAI-compatible request-accounting gateway.

    Remote agent runtimes receive only a short-lived gateway token. Provider
    credentials remain in the PMC controller and are selected independently
    for every physical request.
    """

    def __init__(
        self,
        *,
        bind_host: str,
        public_host: str,
        upstream_base_url: str,
        upstream_model: str,
        accounting: Any,
        max_failovers: int = 2,
        max_rate_limit_wait: float = 30,
        timeout: float = 300,
    ):
        self.bind_host = bind_host
        self.public_host = public_host
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.upstream_model = upstream_model
        self.accounting = accounting
        self.max_failovers = max(0, max_failovers)
        self.max_rate_limit_wait = max(0.0, max_rate_limit_wait)
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        self._turn = 0
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("provider gateway has not started")
        return f"http://{self.public_host}:{self._server.server_port}/v1"

    def start(self) -> ProviderGateway:
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "PMCProviderGateway/1"

            def log_message(self, _format, *_args):
                # Request bodies and authorization headers must never enter logs.
                return

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self._json(404, {"error": {"message": "not found"}})
                    return
                if self.headers.get("Authorization") != f"Bearer {gateway.token}":
                    self._json(401, {"error": {"message": "unauthorized"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 32 * 1024 * 1024:
                        raise ValueError("invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise TypeError("request must be a JSON object")
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._json(400, {"error": {"message": str(exc)}})
                    return
                # The gateway deliberately uses non-streaming responses so one
                # response can be reconciled atomically before returning it.
                payload["stream"] = False
                # LiteLLM removes transport/provider prefixes before calling
                # this OpenAI-compatible endpoint. The controller owns the
                # exact endpoint-native model ID and restores it here.
                payload["model"] = gateway.upstream_model
                messages = payload.get("messages") or []
                max_output = int(
                    payload.get("max_tokens")
                    or payload.get("max_completion_tokens")
                    or 4096
                )
                last_status = 502
                last_message = "provider request failed"
                last_headers: dict[str, str] = {}
                attempts = 0
                waited_for_reset = False
                while attempts <= gateway.max_failovers:
                    with gateway._lock:
                        gateway._turn += 1
                        turn = gateway._turn
                    try:
                        ticket = gateway.accounting.reserve(turn, messages, max_output)
                    except Exception as exc:  # noqa: BLE001 - accounting contract boundary
                        if last_status == 429:
                            delay = (
                                gateway.accounting.db.provider_next_available_seconds(
                                    gateway.accounting.candidate.provider
                                )
                            )
                            if (
                                not waited_for_reset
                                and delay is not None
                                and delay <= gateway.max_rate_limit_wait
                            ):
                                time.sleep(delay + 0.1)
                                attempts = 0
                                waited_for_reset = True
                                continue
                            break
                        self._json(503, {"error": {"message": str(exc)}})
                        return
                    import os

                    key = os.getenv(ticket.api_key_env or "")
                    if ticket.api_key_env and not key:
                        error = RuntimeError(
                            f"credential environment variable is unavailable: {ticket.api_key_env}"
                        )
                        gateway.accounting.fail(ticket, error)
                        self._json(503, {"error": {"message": str(error)}})
                        return
                    try:
                        upstream_headers = {
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        }
                        if key:
                            upstream_headers["Authorization"] = f"Bearer {key}"
                        response = httpx.post(
                            f"{gateway.upstream_base_url}/chat/completions",
                            headers=upstream_headers,
                            json=payload,
                            timeout=gateway.timeout,
                        )
                    except httpx.HTTPError as exc:
                        gateway.accounting.fail(ticket, exc)
                        last_message = type(exc).__name__
                        attempts += 1
                        continue
                    headers = _rate_headers(response.headers)
                    if response.status_code >= 400:
                        detail = ""
                        try:
                            raw_error = response.json().get("error", {})
                            if isinstance(raw_error, dict):
                                detail = str(raw_error.get("message") or "")[:1000]
                        except (ValueError, AttributeError):
                            pass
                        error = ProviderError(
                            response.status_code,
                            f"provider HTTP {response.status_code}: {detail}",
                            headers,
                        )
                        gateway.accounting.fail(ticket, error)
                        last_status = response.status_code
                        last_message = str(error)
                        last_headers = headers
                        attempts += 1
                        if (
                            response.status_code == 429
                            and attempts > gateway.max_failovers
                            and not waited_for_reset
                        ):
                            delay = (
                                gateway.accounting.db.provider_next_available_seconds(
                                    gateway.accounting.candidate.provider
                                )
                            )
                            if (
                                delay is not None
                                and delay <= gateway.max_rate_limit_wait
                            ):
                                time.sleep(delay + 0.1)
                                attempts = 0
                                waited_for_reset = True
                                continue
                        if response.status_code not in {
                            401,
                            403,
                            429,
                            500,
                            502,
                            503,
                            504,
                        }:
                            break
                        continue
                    try:
                        data = response.json()
                    except ValueError:
                        error = RuntimeError("provider returned non-JSON response")
                        gateway.accounting.fail(ticket, error)
                        self._json(502, {"error": {"message": str(error)}})
                        return
                    usage = data.get("usage") or {}
                    raw_cost = usage.get("cost") or data.get("cost")
                    try:
                        cost = float(raw_cost) if raw_cost is not None else None
                    except (TypeError, ValueError):
                        cost = None
                    reply = ChatReply(
                        content="",
                        input_tokens=usage.get("prompt_tokens")
                        or usage.get("input_tokens"),
                        output_tokens=usage.get("completion_tokens")
                        or usage.get("output_tokens"),
                        request_id=response.headers.get("x-request-id")
                        or data.get("id"),
                        cost_usd=cost,
                        rate_headers=headers,
                        raw=data,
                    )
                    gateway.accounting.succeed(ticket, reply)
                    self._json(200, data)
                    return
                self.send_response(last_status)
                for key, value in last_headers.items():
                    self.send_header(key, value)
                body = json.dumps({"error": {"message": last_message}}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        # One OpenHands attempt owns one ephemeral gateway. Serial processing
        # prevents LiteLLM metadata probes/retries from racing the actual call
        # and falsely exhausting a credential's concurrency reservation.
        self._server = HTTPServer((self.bind_host, 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="pmc-provider-gateway",
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()
