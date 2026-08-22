from __future__ import annotations


def classify(request: str) -> str:
    """Cheap cold-start classifier. Real routing learns from outcomes later."""
    t = request.lower()
    if any(x in t for x in ("typo", "rename variable", "change text", "one line")):
        return "TRIVIAL_EDIT"
    if any(x in t for x in ("bug", "fix", "broken", "crash", "regression")):
        return "BUG_FIX"
    if any(x in t for x in ("refactor", "cleanup", "restructure")):
        return "REFACTOR"
    if any(x in t for x in ("test", "coverage")):
        return "TEST_CREATION"
    if any(x in t for x in ("dependency", "upgrade", "api migration", "library")):
        return "DEPENDENCY_API"
    if any(x in t for x in ("architecture", "repo-wide", "across the repo")):
        return "ARCHITECTURAL"
    if any(x in t for x in ("add", "implement", "feature", "support")):
        return "FEATURE"
    return "UNKNOWN"
