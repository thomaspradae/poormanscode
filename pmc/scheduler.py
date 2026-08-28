from __future__ import annotations

import hashlib
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
        require_model_conformance: bool = False,
        router_policy: str = "contextual_thompson",
        contextual_min_observations: int = 50,
        bandit_simulations: int = 256,
    ):
        self.db = db
        self.exploration_rate = exploration_rate
        self.min_samples = min_samples
        self.capability_registry = capability_registry
        self.require_model_conformance = require_model_conformance
        self.router_policy = router_policy
        self.contextual_min_observations = contextual_min_observations
        self.bandit_simulations = bandit_simulations

    def _contextual_thompson(
        self, job: Job, pool: list[Candidate], unavailable: dict[str, str],
        role: str, phase: str, attempt_no: int,
    ) -> SchedulerDecision:
        rows = (
            self.db.contextual_candidate_stats(job, role, phase, repo_specific=True)
            if role == "builder" else self.db.contextual_role_stats(job, role)
        )
        context_level = "repo"
        if not rows and role == "builder":
            rows = self.db.contextual_candidate_stats(job, role, phase, repo_specific=False)
            context_level = "cross_repo"
        stats = {r["candidate"]: r for r in rows}
        seed_text = f"{job.id}:{attempt_no}:{role}:{SCHEDULER_POLICY_VERSION}"
        seed = int(hashlib.sha256(seed_text.encode()).hexdigest()[:16], 16)
        rng = random.Random(seed)
        capacity = {
            provider: self.db.provider_capacity(provider)
            for provider in {c.provider for c in pool if c.provider}
        }

        def draw(candidate: Candidate) -> float:
            row = stats.get(candidate.name, {})
            n = int(row.get("observations") or 0)
            wins = int(row.get("stable_successes") or 0)
            quality = rng.betavariate(1 + wins, 1 + max(0, n - wins))
            attention = min(0.20, float(row.get("human_seconds") or 0) / 3600)
            latency = min(0.10, math.log1p(float(row.get("latency_seconds") or 0)) / 120)
            cost = min(0.20, float(row.get("cost_usd") or candidate.monetary_cost_hint or 0) / 2)
            provider_capacity = capacity.get(candidate.provider or "", {})
            scarcity = 0.05 if provider_capacity and provider_capacity.get("available_concurrency", 0) <= 1 else 0
            return quality - attention - latency - cost - scarcity

        wins = {candidate.name: 0 for candidate in pool}
        for _ in range(max(32, self.bandit_simulations)):
            best = max(pool, key=draw)
            wins[best.name] += 1
        sampled = [(draw(candidate), candidate) for candidate in pool]
        chosen_score, chosen = max(sampled, key=lambda item: item[0])
        simulations = max(32, self.bandit_simulations)
        probability = (wins[chosen.name] + 1) / (simulations + len(pool))
        row = stats.get(chosen.name, {})
        uncertainty = 1 / math.sqrt(2 + int(row.get("observations") or 0))
        return SchedulerDecision(
            chosen, "contextual_thompson", chosen_score,
            "contextual stable-outcome Thompson sample",
            [c.name for c in pool], unavailable, probability,
            SCHEDULER_POLICY_VERSION,
            {"seed": seed, "simulations": simulations,
             "context": {"task_type": job.task_type, "complexity": job.complexity,
                         "risk": job.risk, "phase": phase, "repo": str(job.repo),
                         "level": context_level},
             "propensities": {name: (count + 1) / (simulations + len(pool))
                              for name, count in wins.items()},
             "uncertainty": uncertainty, "context_stats": stats},
        )

    def available(
        self,
        c: Candidate,
        universe: list[Candidate] | None = None,
        job: Job | None = None,
    ) -> Availability:
        if not c.enabled:
            return Availability(False, "disabled")
        if c.provider:
            provider_ok, provider_reason = self.db.provider_availability(c.provider)
            if not provider_ok:
                return Availability(False, provider_reason)
        # Jules is a provider-hosted executor: its tool/runtime conformance is
        # validated by the Jules API path, not by the OpenHands coding ladder.
        # Keep the strict gate for local/OpenHands model candidates.
        if self.require_model_conformance and c.executor != "jules":
            conformance = self.db.model_conformance(c)
            if not conformance:
                return Availability(False, "model conformance unknown")
            if conformance["status"] != "AVAILABLE":
                return Availability(False, f"model {conformance['status'].lower()}")
        provider_evidenced_jules_quota = bool(
            job
            and c.executor == "jules"
            and job.constraints.get("provider_evidenced_jules_quota", False)
        )
        # Football Lab Jules tasks intentionally use provider-reported
        # exhaustion across independent credentials.  The legacy per-candidate
        # gate is a single shared counter and would reject the task before the
        # task-scoped attempt check below can apply.
        if not provider_evidenced_jules_quota:
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
        if (
            c.quota_attempts is not None
            and c.quota_window_seconds is not None
            and not provider_evidenced_jules_quota
        ):
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
            # Jules tasks participate in the same dependency DAG as every
            # other executor.  Dependencies are integrated by the controller
            # before dispatch; rejecting Jules here made every chained task
            # fail with a misleading profile-constraint error.
            # BashExecutor is intentionally a narrow, low-overhead executor.  It is
            # not a general software-engineering runtime: production experience
            # showed that difficult/multi-file work burns turns without converging.
            # A job may opt in explicitly for a controlled experiment, but the
            # default is to block instead of silently wasting its attempt budget.
            if c.executor == "bash" and not c.extra.get(
                "allow_complex_tasks", False
            ):
                mature_task_types = {
                    "FEATURE",
                    "ARCHITECTURAL",
                    "DEPENDENCY_API",
                    "INTEGRATION",
                    "UI",
                }
                if job.complexity == "DIFFICULT" or job.task_type in mature_task_types:
                    return False
            complexities = c.extra.get("complexities")
            if complexities and job.complexity not in complexities:
                return False
            risks = c.extra.get("risks")
            if risks and job.risk not in risks:
                return False
            task_types = c.extra.get("task_types")
            if task_types and job.task_type not in task_types:
                return False
            return not (
                c.extra.get("first_attempt_only", False) and attempt_no > 1
            )

        unavailable: dict[str, str] = {}
        pool = []
        for c in candidates:
            if c.role != role:
                continue
            if c.name in exclude:
                unavailable[c.name] = "excluded by retry policy"
                continue
            availability = self.available(c, candidates, job)
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
        if (
            self.router_policy == "contextual_thompson"
            and self.db.human_observation_count(role) >= self.contextual_min_observations
        ):
            return self._contextual_thompson(
                job, pool, unavailable, role, phase, attempt_no
            )
        stats = self._stats_map(role, job.task_type, phase)
        under_sampled = [
            c
            for c in pool
            if c.extra.get("exploration", True)
            and int(stats.get(c.name, {}).get("attempts", 0)) < self.min_samples
        ]
        if under_sampled:
            minimum = min(int(stats.get(x.name, {}).get("attempts", 0)) for x in under_sampled)
            least_sampled = [x for x in under_sampled if int(stats.get(x.name, {}).get("attempts", 0)) == minimum]
            c = random.choice(least_sampled)
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
