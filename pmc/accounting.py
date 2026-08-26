from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .context_xray import analyze_request_context
from .db import Database


@dataclass(slots=True)
class RequestTicket:
    model_request_id: int
    reservation_id: int
    credential_reservation_id: int | None = None
    credential_id: str | None = None
    api_key_env: str | None = None
    request_kind: str = "agent"
    context_metrics: dict[str, Any] | None = None


class BudgetExceeded(RuntimeError):
    pass


class ContextCapacityExceeded(RuntimeError):
    pass


class ModelRequestAccounting:
    def __init__(
        self,
        db: Database,
        *,
        job_id: str,
        attempt_id: int,
        candidate: Any,
        budget: Any | None = None,
    ):
        self.db, self.job_id, self.attempt_id, self.candidate = (
            db,
            job_id,
            attempt_id,
            candidate,
        )
        self.budget = budget
        self._previous_estimated_input: int | None = None
        self._previous_request_hash: str | None = None
        self._condensation_count = 0

    def reserve(
        self,
        turn: int,
        messages: list[dict[str, Any]],
        max_output: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> RequestTicket:
        # Agent runtimes use structured content blocks, tool calls and tool
        # results.  Estimate the complete serialized conversation rather than
        # only plain-text ``content`` so reservations remain conservative.
        context_metrics = analyze_request_context(
            messages,
            tools,
            model_context_window=(
                int(self.candidate.extra["model_context_window"])
                if self.candidate.extra.get("model_context_window")
                else None
            ),
            previous_estimated_input=self._previous_estimated_input,
            previous_request_hash=self._previous_request_hash,
            condensation_count=self._condensation_count,
        )
        estimated_input = int(context_metrics["estimated_input_tokens"])
        request_soft_limit = self.candidate.extra.get("request_token_soft_limit")
        if request_soft_limit and estimated_input + max_output > int(
            request_soft_limit
        ):
            self.db.event(
                "MODEL_REQUEST_INCOMPATIBLE_CONTEXT",
                job_id=self.job_id,
                attempt_id=self.attempt_id,
                payload={
                    "candidate": self.candidate.name,
                    "turn": turn,
                    "estimated_input_tokens": estimated_input,
                    "estimated_output_tokens": max_output,
                    "request_token_soft_limit": int(request_soft_limit),
                    "request_hash": context_metrics["request_hash"],
                },
            )
            raise ContextCapacityExceeded(
                "request context and output allowance exceed the selected "
                f"lane's sustainable request limit ({estimated_input}+{max_output} > "
                f"{int(request_soft_limit)})"
            )
        estimated_cost = self.candidate.monetary_cost_hint or None
        totals = self.db.job_model_request_totals(
            self.job_id, since_latest_feedback=True
        )
        b = self.budget
        if b:
            if totals["requests"] >= b.max_model_requests:
                raise BudgetExceeded("job model-request budget exhausted")
            if (
                b.max_input_tokens is not None
                and totals["input_tokens"] + estimated_input > b.max_input_tokens
            ):
                raise BudgetExceeded("job input-token budget exhausted")
            if (
                b.max_output_tokens is not None
                and totals["output_tokens"] + max_output > b.max_output_tokens
            ):
                raise BudgetExceeded("job output-token budget exhausted")
            if (
                b.max_cost_usd is not None
                and totals["cost_usd"] + (estimated_cost or 0) > b.max_cost_usd
            ):
                raise BudgetExceeded("job cost budget exhausted")
        credential = None
        if self.candidate.provider:
            ok, reason = self.db.provider_availability(self.candidate.provider)
            if ok and reason != "legacy candidate credential":
                try:
                    credential = self.db.reserve_provider_credential(
                        self.candidate.provider,
                        self.job_id,
                        self.attempt_id,
                        estimated_input + max_output,
                    )
                except RuntimeError:
                    self.db.event(
                        "MODEL_REQUEST_DEFERRED_QUOTA",
                        job_id=self.job_id,
                        attempt_id=self.attempt_id,
                        payload={
                            "candidate": self.candidate.name,
                            "turn": turn,
                            "estimated_tokens": estimated_input + max_output,
                            "next_available_seconds": self.db.provider_next_available_seconds(
                                self.candidate.provider
                            ),
                        },
                    )
                    raise
            elif not ok and reason != "legacy candidate credential":
                raise RuntimeError(
                    f"provider {self.candidate.provider} unavailable: {reason}"
                )
        ids = self.db.reserve_model_request(
            request_key=f"mr-{uuid.uuid4().hex}",
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            candidate=self.candidate,
            turn_number=turn,
            estimated_input=estimated_input,
            estimated_output=max_output,
            estimated_cost=estimated_cost,
            credential_id=credential["credential_id"] if credential else None,
            quota_scope_id=credential["quota_scope_id"] if credential else None,
            context_metrics=context_metrics,
        )
        ticket = RequestTicket(
            *ids,
            credential_reservation_id=credential["reservation_id"]
            if credential
            else None,
            credential_id=credential["credential_id"] if credential else None,
            api_key_env=credential["api_key_env"]
            if credential
            else self.candidate.api_key_env,
            request_kind=str(context_metrics["request_kind"]),
            context_metrics=context_metrics,
        )
        self._previous_estimated_input = estimated_input
        self._previous_request_hash = str(context_metrics["request_hash"])
        self.db.start_model_request(
            ticket.model_request_id, self.job_id, self.attempt_id
        )
        return ticket

    def succeed(self, ticket: RequestTicket, reply: Any) -> None:
        self.db.finish_model_request(
            model_request_id=ticket.model_request_id,
            reservation_id=ticket.reservation_id,
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            candidate=self.candidate,
            reply=reply,
        )
        if ticket.credential_reservation_id:
            self.db.reconcile_provider_credential(
                ticket.credential_reservation_id,
                status_code=None,
                actual_tokens=(reply.input_tokens or 0) + (reply.output_tokens or 0),
                headers=dict(reply.rate_headers or {}),
            )
        if ticket.request_kind == "condenser":
            self._condensation_count += 1

    def fail(self, ticket: RequestTicket, error: Exception) -> None:
        self.db.finish_model_request(
            model_request_id=ticket.model_request_id,
            reservation_id=ticket.reservation_id,
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            candidate=self.candidate,
            error=error,
        )
        if ticket.credential_reservation_id:
            response = getattr(error, "response", None)
            source_headers = (
                getattr(error, "rate_headers", None)
                or getattr(response, "headers", None)
                or getattr(error, "headers", None)
                or {}
            )
            headers = {
                str(key).lower(): str(value)
                for key, value in dict(source_headers).items()
                if str(key).lower().startswith("x-ratelimit-")
                or str(key).lower() in {"retry-after", "request-id", "x-request-id"}
            }
            self.db.reconcile_provider_credential(
                ticket.credential_reservation_id,
                status_code=(
                    getattr(error, "status_code", None)
                    or getattr(response, "status_code", None)
                ),
                actual_tokens=0,
                headers=headers,
            )
