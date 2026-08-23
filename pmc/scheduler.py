from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .db import Database
from .domain import Candidate, Job, SchedulerDecision
from .versioning import SCHEDULER_POLICY_VERSION


class NoAvailableCandidate(RuntimeError):
    def __init__(self, role: str, unavailable: dict[str, str]):
        self.role = role
        self.unavailable = unavailable
        super().__init__(f"No available {role} candidates: {unavailable}")


@dataclass(slots=True)
class Availability:
    ok: bool
    reason: str = ""


class Scheduler:
    """Simple on purpose: cold-start exploration + epsilon-greedy exploitation."""

    def __init__(
        self,
        db: Database,
        exploration_rate: float,
        min_samples: int,
        capability_registry=None,
    ):
        self.db = db
        self.exploration_rate = exploration_rate
        self.min_samples = min_samples
        self.capability_registry = capability_registry

    def available(
        self, c: Candidate, universe: list[Candidate] | None = None
    ) -> Availability:
        if not c.enabled:
            return Availability(False, "disabled")
        quota_ok, quota_reason = self.db.quota_availability(c.name)
        if not quota_ok:
            return Availability(False, quota_reason)
        universe = universe or [c]
        if (
            c.max_concurrency is not None
            and self.db.active_count(c.name) >= c.max_concurrency
        ):
            return Availability(False, "candidate concurrency full")
        if c.resource_group and c.resource_concurrency is not None:
            if c.resource_concurrency == 1 and self.db.resource_busy(c.resource_group):
                return Availability(False, f"resource {c.resource_group} leased")
            group_names = [
                x.name for x in universe if x.resource_group == c.resource_group
            ]
            if self.db.active_count_many(group_names) >= c.resource_concurrency:
                return Availability(False, f"resource {c.resource_group} busy")
        requested = c.extra.get("resource_requirements") or {}
        capacity = c.extra.get("resource_capacity") or {}
        if c.resource_group and requested:
            ok, reason = self.db.quantitative_resource_available(
                c.resource_group, requested, capacity
            )
            if not ok:
                return Availability(False, reason)
        quota_names = (
            [x.name for x in universe if x.quota_group == c.quota_group]
            if c.quota_group
            else [c.name]
        )
        if c.quota_attempts is not None and c.quota_window_seconds is not None:
            used = self.db.attempts_many_in_window(quota_names, c.quota_window_seconds)
            if used >= c.quota_attempts:
                return Availability(False, "rolling attempt quota exhausted")
        quota_tokens = c.extra.get("quota_tokens")
        if quota_tokens is not None and c.quota_window_seconds is not None:
            used_tokens = self.db.tokens_many_in_window(
                quota_names, c.quota_window_seconds
            ) + self.db.reserved_tokens(quota_names)
            if used_tokens >= int(quota_tokens):
                return Availability(False, "rolling token quota exhausted")
        return Availability(True)

    def _stats_map(self, role: str, task_type: str, phase: str) -> dict[str, dict]:
        generic = {
            x["candidate"]: x for x in self.db.candidate_stats(role=role, phase=phase)
        }
        specific = {
            x["candidate"]: x
            for x in self.db.candidate_stats(
                role=role, task_type=task_type, phase=phase
            )
        }
        out = dict(generic)
        for name, row in specific.items():
            if row["attempts"] >= 3:
                out[name] = row
        return out

    def choose(
        self,
        job: Job,
        candidates: list[Candidate],
        *,
        role: str = "builder",
        exclude: set[str] | None = None,
        attempt_no: int = 1,
    ) -> SchedulerDecision:
        exclude = exclude or set()

        def fits(c: Candidate) -> bool:
            if c.executor == "jules" and job.constraints.get("_feature_dependencies"):
                return False
            task_types = c.extra.get("task_types")
            if task_types and job.task_type not in task_types:
                return False
            if c.extra.get("first_attempt_only", False) and attempt_no > 1:
                return False
            return True

        unavailable: dict[str, str] = {}
        pool = []
        for c in candidates:
            if c.role != role:
                continue
            if c.name in exclude:
                unavailable[c.name] = "excluded by retry policy"
                continue
            availability = self.available(c, candidates)
            if not availability.ok:
                unavailable[c.name] = availability.reason
            elif self.capability_registry and self.capability_registry.missing(job, c):
                unavailable[c.name] = "missing capabilities: " + ", ".join(
                    self.capability_registry.missing(job, c)
                )
            elif not fits(c):
                unavailable[c.name] = "task/profile constraint"
            else:
                pool.append(c)
        if not pool:
            raise NoAvailableCandidate(role, unavailable)

        ordered = job.constraints.get("_candidate_order") or []
        if ordered:
            by_name = {candidate.name: candidate for candidate in pool}
            for name in ordered:
                if name in by_name:
                    candidate = by_name[name]
                    return SchedulerDecision(
                        candidate,
                        "ordered",
                        0.5,
                        "first available candidate in job order",
                        [c.name for c in pool],
                        unavailable,
                        1.0,
                        SCHEDULER_POLICY_VERSION,
                        {"candidate_order": ordered},
                    )

        phase = "first" if attempt_no == 1 else "repair"
        stats = self._stats_map(role, job.task_type, phase)
        under_sampled = [
            c
            for c in pool
            if c.extra.get("exploration", True)
            and int(stats.get(c.name, {}).get("attempts", 0)) < self.min_samples
        ]
        if under_sampled:
            c = min(
                under_sampled,
                key=lambda x: int(stats.get(x.name, {}).get("attempts", 0)),
            )
            return SchedulerDecision(
                c,
                "cold_start",
                0.5,
                "candidate needs production observations",
                [x.name for x in pool],
                unavailable,
                1.0,
                SCHEDULER_POLICY_VERSION,
                {"exploration_rate": self.exploration_rate, "phase": phase},
            )

        scored: list[tuple[float, Candidate, str]] = []
        for c in pool:
            row = stats.get(c.name, {})
            n = max(0, int(row.get("attempts", 0) or 0))
            human_n = max(0, int(row.get("human_labeled", 0) or 0))
            if human_n >= 3:
                quality_n = human_n
                wins = max(0, int(row.get("accepted_attempts", 0) or 0))
                signal = "human_accept"
            else:
                quality_n = n
                wins = max(0, int(row.get("verified", 0) or 0))
                signal = "verified"
            # Beta(1,1) posterior mean: deliberately simple until production data is large.
            p = (wins + 1) / (quality_n + 2)
            latency = float(row.get("avg_seconds") or 0.0)
            cost = float(row.get("avg_cost") or c.monetary_cost_hint or 0.0)
            latency_penalty = min(0.12, math.log1p(latency) / 100.0)
            cost_penalty = min(0.25, cost / 2.0)
            score = p - latency_penalty - cost_penalty
            scored.append(
                (
                    score,
                    c,
                    f"signal={signal}, posterior_success={p:.3f}, latency={latency:.1f}s, cost=${cost:.4f}",
                )
            )
        scored.sort(key=lambda x: x[0], reverse=True)

        if len(scored) > 1 and random.random() < self.exploration_rate:
            randomized = [x for x in scored if x[1].extra.get("exploration", True)]
            if randomized:
                score, c, detail = random.choice(randomized)
                return SchedulerDecision(
                    c,
                    "explore",
                    score,
                    "randomized production exploration; " + detail,
                    [x.name for x in pool],
                    unavailable,
                    self.exploration_rate / len(randomized),
                    SCHEDULER_POLICY_VERSION,
                    {"exploration_rate": self.exploration_rate, "phase": phase},
                )

        score, c, detail = scored[0]
        probability = 1.0 if len(scored) == 1 else 1.0 - self.exploration_rate
        return SchedulerDecision(
            c,
            "exploit",
            score,
            "best observed utility; " + detail,
            [x.name for x in pool],
            unavailable,
            probability,
            SCHEDULER_POLICY_VERSION,
            {"exploration_rate": self.exploration_rate, "phase": phase},
        )
