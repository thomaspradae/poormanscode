from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from .domain import Candidate, ExecutionRequest, Job, ReviewResult
from .executors import build_executor
from .gitops import WorktreeManager
from .prompts import reviewer_prompt


class ReviewerService:
    def review(
        self,
        *,
        job: Job,
        worktree: Path,
        candidate: Candidate,
        attempt_no: int,
        verification_summary: str,
    ) -> ReviewResult:
        diff = WorktreeManager(worktree.parent).diff(worktree, job.baseline_commit or "HEAD")
        with tempfile.TemporaryDirectory(prefix="pmc-review-") as td:
            review_dir = Path(td) / "repo"
            shutil.copytree(worktree, review_dir, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            req = ExecutionRequest(
                job=job,
                candidate=candidate,
                worktree=review_dir,
                prompt=reviewer_prompt(job, diff, verification_summary),
                attempt_no=attempt_no,
            )
            result = build_executor(candidate.executor).run(req)
            output = review_dir / "PMC_REVIEW.json"
            if not result.ok or not output.exists():
                return ReviewResult(False, "REJECT", result.error or "reviewer did not produce PMC_REVIEW.json", [])
            try:
                data = json.loads(output.read_text())
                verdict = str(data.get("verdict", "REJECT")).upper()
                findings = [str(x) for x in data.get("findings", [])]
                summary = str(data.get("summary", ""))
                return ReviewResult(verdict == "ACCEPT", verdict, summary, findings)
            except Exception as exc:
                return ReviewResult(False, "REJECT", f"invalid reviewer output: {exc}", [])
