from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .db import Database


@dataclass(slots=True)
class RequestTicket:
    model_request_id: int
    reservation_id: int


class ModelRequestAccounting:
    def __init__(self, db: Database, *, job_id: str, attempt_id: int, candidate: Any):
        self.db, self.job_id, self.attempt_id, self.candidate = (
            db,
            job_id,
            attempt_id,
            candidate,
        )

    def reserve(
        self, turn: int, messages: list[dict[str, str]], max_output: int
    ) -> RequestTicket:
        estimated_input = max(1, sum(len(m.get("content", "")) for m in messages) // 4)
        estimated_cost = self.candidate.monetary_cost_hint or None
        ids = self.db.reserve_model_request(
            request_key=f"mr-{uuid.uuid4().hex}",
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            candidate=self.candidate,
            turn_number=turn,
            estimated_input=estimated_input,
            estimated_output=max_output,
            estimated_cost=estimated_cost,
        )
        ticket = RequestTicket(*ids)
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

    def fail(self, ticket: RequestTicket, error: Exception) -> None:
        self.db.finish_model_request(
            model_request_id=ticket.model_request_id,
            reservation_id=ticket.reservation_id,
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            candidate=self.candidate,
            error=error,
        )
