from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import Candidate, Job
from .providers import OpenAICompatibleClient


@dataclass(frozen=True, slots=True)
class IntelligencePlan:
    planners: int
    reviewers: int
    challenger: bool
    reason: str
    uncertainty: float


def allocate_intelligence(job: Job, *, verification_strong: bool,
                          observations: int) -> IntelligencePlan:
    uncertainty = 1 / (2 + observations) ** 0.5
    high_risk = job.risk in {"HIGH", "CRITICAL"}
    difficult = job.complexity == "DIFFICULT"
    planners = min(job.budget.max_parallel_candidates, 2 if high_risk and difficult else int(difficult))
    reviewers = min(job.budget.max_reviews, 2 if high_risk else int(not verification_strong or job.risk == "MEDIUM"))
    challenger = bool(job.budget.allow_challenger and (high_risk or (difficult and uncertainty > 0.35)))
    reasons = []
    if difficult:
        reasons.append("difficult task")
    if high_risk:
        reasons.append("high semantic risk")
    if not verification_strong:
        reasons.append("weak deterministic verification")
    if uncertainty > 0.35:
        reasons.append("sparse contextual evidence")
    return IntelligencePlan(planners, reviewers, challenger, ", ".join(reasons) or "deterministic gates sufficient", uncertainty)


def advisory_call(candidate: Candidate, accounting: Any, prompt: str) -> str:
    ticket = accounting.reserve(0, [{"role": "user", "content": prompt}], 2048)
    client = OpenAICompatibleClient(candidate.base_url or "", ticket.api_key_env)
    try:
        reply = client.chat(model=candidate.model or "", messages=[{"role": "user", "content": prompt}],
                            temperature=0, max_tokens=2048,
                            extra_body=candidate.extra.get("request_extra"))
    except Exception as exc:
        accounting.fail(ticket, exc)
        raise
    accounting.succeed(ticket, reply)
    return reply.content
