from dataclasses import replace
from pathlib import Path

from pmc.budget import BUDGETS
from pmc.db import Database
from pmc.domain import Candidate, ExecutionResult, Job
from pmc.intelligence import allocate_intelligence
from pmc.scheduler import Scheduler


def _labeled(db: Database, job_id: str, candidate: Candidate, accepted: bool) -> None:
    job = Job(job_id, db.path.parent, "task", task_type="BUG_FIX",
              complexity="STANDARD", risk="MEDIUM")
    db.create_job(job)
    attempt = db.begin_attempt(job.id, 1, candidate, "forced", 0)
    db.finish_attempt(attempt, "READY", ExecutionResult(True), 1, outcome="SUCCESS")
    db.add_feedback(job.id, "ACCEPT" if accepted else "REJECT", attempt_id=attempt)


def test_contextual_bandit_is_reconstructable_and_has_propensity(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    a = Candidate("a", "bash")
    b = Candidate("b", "bash")
    for i in range(8):
        _labeled(db, f"PMC-{i:06d}", a if i < 4 else b, accepted=i < 4)
    target = Job("PMC-T", tmp_path, "task", task_type="BUG_FIX",
                 complexity="STANDARD", risk="MEDIUM")
    db.create_job(target)
    scheduler = Scheduler(db, 0.2, 2, router_policy="contextual_thompson",
                          contextual_min_observations=1, bandit_simulations=128)
    decision = scheduler.choose(target, [a, b])
    assert decision.mode == "contextual_thompson"
    assert 0 < decision.selection_probability <= 1
    assert decision.snapshot["seed"]
    assert set(decision.snapshot["propensities"]) == {"a", "b"}


def test_post_acceptance_regression_removes_stable_success(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite")
    candidate = Candidate("a", "bash")
    job = Job("PMC-X", tmp_path, "task", task_type="BUG_FIX")
    db.create_job(job)
    attempt = db.begin_attempt(job.id, 1, candidate, "forced", 0)
    db.finish_attempt(attempt, "READY", ExecutionResult(True), 1, outcome="SUCCESS")
    db.complete_acceptance(job.id, attempt, "abc")
    db.record_human_metrics(job.id, attempt, review_seconds=12, changed_lines=0,
                            accepted_without_edit=True)
    db.record_post_acceptance_outcome(job.id, "STABLE", details="survived observation window")
    before = db.contextual_candidate_stats(job, "builder", "all")[0]
    assert before["stable_successes"] == 1
    db.record_post_acceptance_outcome(job.id, "REGRESSION", details="broke later")
    after = db.contextual_candidate_stats(job, "builder", "all")[0]
    assert after["stable_successes"] == 0
    assert db.attention_stats()[0]["avg_review_seconds"] == 12


def test_extra_intelligence_has_a_defined_reason_and_budget_ceiling():
    high = Job("x", Path("."), "security migration", complexity="DIFFICULT",
               risk="HIGH", budget=BUDGETS["high-risk"])
    plan = allocate_intelligence(high, verification_strong=False, observations=0)
    assert plan.planners == 2
    assert plan.reviewers == 2
    assert plan.challenger
    low = replace(high, complexity="TRIVIAL", risk="LOW", budget=BUDGETS["trivial"])
    plan = allocate_intelligence(low, verification_strong=True, observations=100)
    assert (plan.planners, plan.reviewers, plan.challenger) == (0, 0, False)
