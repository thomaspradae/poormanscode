from __future__ import annotations

from .domain import BudgetEnvelope

BUDGETS = {
    "trivial": BudgetEnvelope("trivial", 2, 12, 40_000, 12_000, 1200, None, 1, 0, 1, False),
    "standard": BudgetEnvelope("standard", 3, 30, 120_000, 40_000, 3600, None, 1, 1, 2, False),
    "difficult": BudgetEnvelope("difficult", 4, 60, 300_000, 100_000, 7200, None, 2, 1, 3, True),
    "high-risk": BudgetEnvelope("high-risk", 5, 100, 500_000, 180_000, 10800, None, 2, 2, 3, True),
}


def characterize(request: str, task_type: str, priority: int = 2) -> tuple[str, str]:
    text = request.lower()
    high_risk = any(
        term in text
        for term in (
            "auth", "payment", "security", "secret", "migration", "database",
            "release", "deployment", "delete", "permission", "encryption",
        )
    )
    critical = priority == 0 or any(term in text for term in ("production", "critical", "blocking"))
    difficult = task_type in {"ARCHITECTURAL", "DEPENDENCY_API"} or any(
        term in text for term in ("architecture", "repo-wide", "from scratch", "unknown api")
    )
    trivial = task_type == "TRIVIAL_EDIT" and not high_risk
    complexity = "TRIVIAL" if trivial else ("DIFFICULT" if difficult else "STANDARD")
    risk = "CRITICAL" if critical and high_risk else ("HIGH" if high_risk or critical else "MEDIUM")
    return complexity, risk


def envelope_for(complexity: str, risk: str, override: str | None = None) -> BudgetEnvelope:
    if override:
        return BUDGETS[override]
    if risk in {"HIGH", "CRITICAL"}:
        return BUDGETS["high-risk"]
    if complexity == "DIFFICULT":
        return BUDGETS["difficult"]
    if complexity == "TRIVIAL":
        return BUDGETS["trivial"]
    return BUDGETS["standard"]
