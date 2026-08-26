from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .config import PMCConfig
from .db import Database
from .domain import Candidate
from .providers import OpenAICompatibleClient, ProviderError


@dataclass(frozen=True, slots=True)
class CredentialProbeResult:
    credential_id: str
    provider: str
    health: str
    http_status: int | None
    latency_seconds: float
    quota_scope_id: str | None
    quota_scope_confidence: str
    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    reset_at: str | None = None
    error: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        return asdict(self)


def _candidate_for_provider(cfg: PMCConfig, provider: str) -> Candidate | None:
    eligible = [
        candidate
        for candidate in cfg.candidates
        if candidate.provider == provider and candidate.base_url and candidate.model
    ]
    eligible.sort(
        key=lambda candidate: (
            not candidate.enabled,
            candidate.executor != "openhands",
            candidate.name,
        )
    )
    return eligible[0] if eligible else None


def _rate_headers(headers: Any) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in dict(headers).items()
        if str(key).lower().startswith("x-ratelimit")
        or str(key).lower() in {"retry-after", "x-request-id", "request-id"}
    }


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, ProviderError):
        return str(exc)[:500]
    if isinstance(exc, httpx.HTTPStatusError):
        return f"provider HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {str(exc)[:400]}"


def probe_credential(
    db: Database, cfg: PMCConfig, credential: dict[str, Any]
) -> CredentialProbeResult:
    credential_id = str(credential["credential_id"])
    provider = str(credential["provider"])
    reservation_id, probe_id = db.reserve_specific_credential_probe(credential_id)
    db.event(
        "CREDENTIAL_PROBE_STARTED",
        payload={"credential_id": credential_id, "provider": provider},
    )
    started = time.monotonic()
    status: int | None = None
    headers: dict[str, str] = {}
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    state = "FAILED"
    try:
        env_name = str(credential["api_key_env"])
        if not os.getenv(env_name):
            raise RuntimeError("credential environment variable is unavailable")
        if provider == "jules":
            response = httpx.get(
                "https://jules.googleapis.com/v1alpha/sources",
                headers={"x-goog-api-key": os.environ[env_name]},
                timeout=30,
            )
            headers = _rate_headers(response.headers)
            request_id = response.headers.get("x-request-id")
            status = response.status_code
            response.raise_for_status()
        else:
            candidate = _candidate_for_provider(cfg, provider)
            if candidate is None:
                raise RuntimeError(
                    f"no probe model is configured for provider {provider}"
                )
            reply = OpenAICompatibleClient(
                str(candidate.base_url), env_name, timeout=60
            ).chat(
                model=str(candidate.model),
                messages=[
                    {
                        "role": "user",
                        "content": "Reply with exactly PMC_CREDENTIAL_OK",
                    }
                ],
                temperature=0,
                max_tokens=32,
            )
            status = 200
            headers = reply.rate_headers
            request_id = reply.request_id
            input_tokens = reply.input_tokens
            output_tokens = reply.output_tokens
        state = "SUCCEEDED"
    except ProviderError as exc:
        status = exc.status_code
        headers = exc.rate_headers
        error = _safe_error(exc)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        headers = _rate_headers(exc.response.headers)
        request_id = exc.response.headers.get("x-request-id")
        error = _safe_error(exc)
    except (httpx.HTTPError, RuntimeError) as exc:
        status = 503
        error = _safe_error(exc)

    latency = time.monotonic() - started
    db.reconcile_provider_credential(
        reservation_id,
        status_code=status,
        actual_tokens=(input_tokens or 0) + (output_tokens or 0),
        headers=headers,
    )
    db.finish_credential_probe(
        probe_id,
        state=state,
        http_status=status,
        latency_seconds=latency,
        request_id=request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        rate_headers=headers,
        error=error,
    )
    event = (
        "CREDENTIAL_PROBE_SUCCEEDED"
        if state == "SUCCEEDED"
        else "CREDENTIAL_PROBE_FAILED"
    )
    db.event(
        event,
        payload={
            "credential_id": credential_id,
            "provider": provider,
            "http_status": status,
            "latency_seconds": round(latency, 3),
        },
    )
    refreshed = next(
        row
        for row in db.credentials_for_probe({provider}, include_all=True)
        if row["credential_id"] == credential_id
    )
    return CredentialProbeResult(
        credential_id=credential_id,
        provider=provider,
        health=str(refreshed["health"]),
        http_status=status,
        latency_seconds=round(latency, 3),
        quota_scope_id=refreshed.get("quota_scope_id"),
        quota_scope_confidence=str(refreshed["quota_scope_confidence"]),
        requests_remaining=refreshed.get("requests_remaining"),
        tokens_remaining=refreshed.get("tokens_remaining"),
        reset_at=refreshed.get("reset_at"),
        error=error,
    )


def probe_credentials(
    db: Database,
    cfg: PMCConfig,
    providers: set[str] | None = None,
    *,
    include_all: bool = False,
    concurrency: int = 2,
) -> list[CredentialProbeResult]:
    credentials = db.credentials_for_probe(providers, include_all=include_all)
    if not credentials:
        return []
    workers = max(1, min(int(concurrency), 4, len(credentials)))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="pmc-credential"
    ) as pool:
        results = list(
            pool.map(lambda item: probe_credential(db, cfg, item), credentials)
        )
    return sorted(results, key=lambda result: (result.provider, result.credential_id))
