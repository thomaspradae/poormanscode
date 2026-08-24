from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from .db import Database


@dataclass(slots=True)
class RequestTicket:
    model_request_id: int
    reservation_id: int
    credential_reservation_id: int | None = None
    credential_id: str | None = None
    api_key_env: str | None = None


class BudgetExceeded(RuntimeError):
    pass


class ModelRequestAccounting:
    def __init__(self, db: Database, *, job_id: str, attempt_id: int, candidate: Any,
                 budget: Any | None = None):
        self.db, self.job_id, self.attempt_id, self.candidate = (
            db,
            job_id,
            attempt_id,
            candidate,
        )
        self.budget = budget

    def reserve(
        self, turn: int, messages: list[dict[str, Any]], max_output: int
    ) -> RequestTicket:
        serialized_chars = 0
        for message in messages:
            serialized_chars += len(message.get("content") or "")
            if message.get("tool_calls"):
                serialized_chars += len(
                    json.dumps(message["tool_calls"], separators=(",", ":"))
                )
        estimated_input = max(1, serialized_chars // 4)
        estimated_cost = self.candidate.monetary_cost_hint or None
        totals = self.db.job_model_request_totals(
            self.job_id, since_latest_feedback=True
        )
        b = self.budget
        if b:
            if totals["requests"] >= b.max_model_requests:
                raise BudgetExceeded("job model-request budget exhausted")
            if b.max_input_tokens is not None and totals["input_tokens"] + estimated_input > b.max_input_tokens:
                raise BudgetExceeded("job input-token budget exhausted")
            if b.max_output_tokens is not None and totals["output_tokens"] + max_output > b.max_output_tokens:
                raise BudgetExceeded("job output-token budget exhausted")
            if b.max_cost_usd is not None and totals["cost_usd"] + (estimated_cost or 0) > b.max_cost_usd:
                raise BudgetExceeded("job cost budget exhausted")
        credential = None
        if self.candidate.provider:
            ok, reason = self.db.provider_availability(self.candidate.provider)
            if ok and reason != "legacy candidate credential":
                credential = self.db.reserve_provider_credential(
                    self.candidate.provider, self.job_id, self.attempt_id,
                    estimated_input + max_output,
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
        )
        ticket = RequestTicket(
            *ids,
            credential_reservation_id=credential["reservation_id"] if credential else None,
            credential_id=credential["credential_id"] if credential else None,
            api_key_env=credential["api_key_env"] if credential else self.candidate.api_key_env,
        )
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
                ticket.credential_reservation_id, status_code=None,
                actual_tokens=(reply.input_tokens or 0) + (reply.output_tokens or 0),
                headers=dict(reply.rate_headers or {}),
            )

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
            self.db.reconcile_provider_credential(
                ticket.credential_reservation_id,
                status_code=getattr(error, "status_code", None), actual_tokens=0,
                headers=dict(getattr(error, "rate_headers", {}) or {}),
            )
