from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .domain import Job


def _default(obj: Any):
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "value"):
        return obj.value
    if is_dataclass(obj):
        return asdict(obj)
    return str(obj)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Reporter:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        p = self.root / job_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def json(self, job_id: str, name: str, data: Any) -> Path:
        path = self.job_dir(job_id) / name
        _atomic_write(path, json.dumps(data, indent=2, default=_default, sort_keys=True) + "\n")
        return path

    def text(self, job_id: str, name: str, text: str) -> Path:
        path = self.job_dir(job_id) / name
        _atomic_write(path, text)
        return path

    def record_job(self, job: Job) -> None:
        self.json(job.id, "job.json", job)

    def record_attempt(self, job_id: str, attempt_no: int, candidate: Any, decision: Any, result: Any) -> None:
        self.json(
            job_id,
            f"attempt-{attempt_no:02d}.json",
            {"candidate": candidate, "decision": decision, "result": result},
        )

    def record_verification(self, job_id: str, attempt_no: int, verification: Any) -> None:
        self.json(job_id, f"verification-{attempt_no:02d}.json", verification)

    def record_review(self, job_id: str, attempt_no: int, review: Any) -> None:
        self.json(job_id, f"review-{attempt_no:02d}.json", review)

    def final_diff(self, job_id: str, diff: str) -> None:
        self.text(job_id, "final.diff", diff)

    def summary(self, job: Job, commit: str | None = None) -> None:
        lines = [
            f"# {job.id}",
            "",
            f"State: {job.state.value}",
            f"Repository: {job.repo}",
            f"Request: {job.request}",
        ]
        if job.acceptance:
            lines += ["", "Acceptance:"] + [f"- {x}" for x in job.acceptance]
        if job.worktree:
            lines += ["", f"Worktree: {job.worktree}"]
        if commit:
            lines += [f"Commit: {commit}"]
        self.text(job.id, "summary.md", "\n".join(lines) + "\n")
