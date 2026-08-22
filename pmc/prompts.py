from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .context import build_context_packet
from .domain import Job


def builder_prompt(job: Job, worktree: Path, repo_cfg: dict[str, Any], feedback: str = "") -> str:
    context = build_context_packet(worktree, job.request)
    visible_checks = {
        k: repo_cfg.get(k)
        for k in ("test", "lint", "typecheck", "build")
        if repo_cfg.get(k)
    }
    # hidden_test is deliberately absent.
    parts = [
        "You are a coding worker inside an isolated task worktree.",
        "Implement the ticket completely. Inspect the repository before changing code.",
        "Do not commit, push, rewrite Git history, weaken tests, delete tests just to pass, or expose secrets.",
        "Keep the patch narrowly scoped. The controller, not you, decides whether the work is accepted.",
        f"TICKET:\n{job.request}",
    ]
    if job.acceptance:
        parts.append("ACCEPTANCE CRITERIA:\n- " + "\n- ".join(job.acceptance))
    if job.constraints:
        parts.append("CONSTRAINTS:\n" + json.dumps(job.constraints, indent=2))
    if visible_checks:
        parts.append("VISIBLE VERIFICATION COMMANDS:\n" + json.dumps(visible_checks, indent=2))
    if context:
        parts.append(context)
    if feedback:
        parts.append("EVIDENCE FROM EARLIER ATTEMPTS / HUMAN FEEDBACK:\n" + feedback)
    return "\n\n".join(parts)


def reviewer_prompt(job: Job, diff_text: str, verification_summary: str) -> str:
    return f"""You are an independent code reviewer. You did not author this patch.
Do not modify the repository. Review only against the ticket, acceptance criteria, correctness,
backwards compatibility, security, and unjustified scope. Mechanical tests are reported separately.
Write a file named PMC_REVIEW.json containing exactly a JSON object with keys:
verdict (ACCEPT or REJECT), summary (string), findings (array of strings).

TICKET:
{job.request}

ACCEPTANCE:
{json.dumps(job.acceptance, indent=2)}

VERIFICATION:
{verification_summary}

DIFF:
{diff_text[:80_000]}
"""
