from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .domain import Job
from .gitops import git, intent_to_add_untracked
from .versioning import stable_hash

WORK_STATE_VERSION = "work-state-v1"
_MAX_DIFF_CHARS = 32_000
_MAX_FAILURE_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class WorkState:
    """Model-independent recovery state for an in-progress repository task.

    This deliberately records repository truth and controller evidence, not an
    agent's private reasoning or raw conversation history.
    """

    version: str
    task: str
    acceptance: list[str]
    baseline_commit: str | None
    changed_files: list[str]
    git_status: list[str]
    current_diff: str
    diff_hash: str | None
    current_plan: list[str] = field(default_factory=list)
    latest_failure: str | None = None
    unresolved_blockers: list[str] = field(default_factory=list)
    evidence_hash: str | None = None

    @property
    def has_partial_work(self) -> bool:
        return bool(self.changed_files or self.current_diff.strip())

    @property
    def content_hash(self) -> str:
        return stable_hash(asdict(self))

    def manifest(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hash": self.content_hash,
            "baseline_commit": self.baseline_commit,
            "changed_files": self.changed_files,
            "diff_hash": self.diff_hash,
            "has_partial_work": self.has_partial_work,
            "latest_failure_present": self.latest_failure is not None,
            "evidence_hash": self.evidence_hash,
        }

    def prompt_packet(self) -> str:
        lines = [
            "MODEL-INDEPENDENT WORK STATE:",
            "Treat this as controller-observed repository truth. Do not repeat completed work.",
            f"Baseline: {self.baseline_commit or 'unknown'}",
            "Acceptance criteria:",
            *(f"- {item}" for item in self.acceptance),
            "Changed files:",
            *(f"- {item}" for item in self.changed_files),
        ]
        if not self.changed_files:
            lines.append("- none")
        if self.current_plan:
            lines.extend(
                ["Controller plan:", *(f"- {item}" for item in self.current_plan)]
            )
        if self.latest_failure:
            lines.extend(["Latest exact failure/evidence:", self.latest_failure])
        if self.unresolved_blockers:
            lines.extend(
                [
                    "Unresolved blockers:",
                    *(f"- {item}" for item in self.unresolved_blockers),
                ]
            )
        if self.current_diff:
            lines.extend(
                ["Current partial diff (preserve useful work):", self.current_diff]
            )
        return "\n".join(lines)


def build_work_state(
    job: Job,
    worktree: Path,
    *,
    failures: list[str] | None = None,
    current_plan: list[str] | None = None,
) -> WorkState:
    baseline = job.baseline_commit
    # Make newly created files visible to Git diff without staging their content.
    intent_to_add_untracked(worktree)
    status = git(worktree, "status", "--short").stdout.splitlines()
    changed = (
        git(worktree, "diff", "--name-only", baseline, "--").stdout.splitlines()
        if baseline
        else []
    )
    diff = (
        git(worktree, "diff", "--unified=2", baseline, "--").stdout[:_MAX_DIFF_CHARS]
        if baseline
        else ""
    )
    failures = failures or []
    latest = failures[-1][:_MAX_FAILURE_CHARS] if failures else None
    blockers = [
        item[:1000]
        for item in failures[-3:]
        if any(word in item.lower() for word in ("block", "missing", "unavailable"))
    ]
    return WorkState(
        version=WORK_STATE_VERSION,
        task=job.request,
        acceptance=list(job.acceptance),
        baseline_commit=baseline,
        changed_files=sorted(set(changed)),
        git_status=status[:500],
        current_diff=diff,
        diff_hash=stable_hash(diff) if diff else None,
        current_plan=list(current_plan or []),
        latest_failure=latest,
        unresolved_blockers=blockers,
        evidence_hash=stable_hash(failures[-3:]) if failures else None,
    )
