from __future__ import annotations

import os
import re
import fnmatch
import shutil
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
    )
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed:\n{p.stderr.strip()}")
    return p


def ensure_repo(repo: Path) -> None:
    if git(repo, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise GitError(f"Not a Git repository: {repo}")


def ensure_clean(repo: Path) -> None:
    status = git(repo, "status", "--porcelain").stdout.strip()
    if status:
        raise GitError(
            "Source repository is not clean. Commit/stash changes before creating a new PMC job.\n"
            + status
        )


def resolve_commit(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref).stdout.strip()


def safe_branch(job_id: str) -> str:
    return "pmc/" + re.sub(r"[^A-Za-z0-9._-]+", "-", job_id.lower())

DEFAULT_EPHEMERAL = [
    ".pytest_cache/*", "*/.pytest_cache/*",
    "__pycache__/*", "*/__pycache__/*", "*.pyc", "*/*.pyc",
    ".ruff_cache/*", "*/.ruff_cache/*",
    ".mypy_cache/*", "*/.mypy_cache/*",
    ".coverage", "htmlcov/*",
    "node_modules/*", "*/node_modules/*",
]

def intent_to_add_untracked(worktree: Path, ignore_patterns: list[str] | None = None) -> None:
    """Make meaningful untracked files visible to git diff without staging contents."""
    out = git(worktree, "ls-files", "--others", "--exclude-standard").stdout
    patterns = DEFAULT_EPHEMERAL + list(ignore_patterns or [])
    files = []
    for x in out.splitlines():
        if not x.strip():
            continue
        if any(fnmatch.fnmatch(x, pat) for pat in patterns):
            continue
        files.append(x)
    if files:
        git(worktree, "add", "-N", "--", *files)


class WorktreeManager:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def create(self, repo: Path, job_id: str, base_branch: str) -> tuple[Path, str]:
        ensure_repo(repo)
        ensure_clean(repo)
        baseline = resolve_commit(repo, base_branch)
        path = self.root / job_id
        if path.exists():
            raise GitError(f"Worktree path already exists: {path}")
        branch = safe_branch(job_id)
        # Remove stale branch from an earlier destroyed worktree.
        git(repo, "branch", "-D", branch, check=False)
        p = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(path), baseline],
            text=True,
            capture_output=True,
        )
        if p.returncode != 0:
            raise GitError(p.stderr.strip())
        return path, baseline

    def normalize_worker_commits(self, worktree: Path, baseline: str) -> None:
        # Always clear worker-owned index/commits while preserving working-tree content.
        # This also removes accidental `git add` of generated test artifacts.
        git(worktree, "reset", "--mixed", baseline)

    def diff(self, worktree: Path, baseline: str) -> str:
        intent_to_add_untracked(worktree)
        return git(worktree, "diff", "--binary", baseline, "--").stdout

    def changed_files(self, worktree: Path, baseline: str) -> list[str]:
        intent_to_add_untracked(worktree)
        out = git(worktree, "diff", "--name-only", baseline, "--").stdout
        return [x for x in out.splitlines() if x.strip()]

    def apply_patch(self, worktree: Path, patch: str) -> None:
        p = subprocess.run(
            ["git", "-C", str(worktree), "apply", "--3way", "--whitespace=nowarn", "-"],
            input=patch,
            text=True,
            capture_output=True,
        )
        if p.returncode != 0:
            # A normal apply can succeed when 3-way cannot because the patch has no index metadata.
            p = subprocess.run(
                ["git", "-C", str(worktree), "apply", "--whitespace=nowarn", "-"],
                input=patch,
                text=True,
                capture_output=True,
            )
        if p.returncode != 0:
            raise GitError(f"Could not apply executor patch:\n{p.stderr.strip()}")

    def commit(self, worktree: Path, message: str) -> str:
        git(worktree, "add", "-A")
        if not git(worktree, "diff", "--cached", "--quiet", check=False).returncode:
            raise GitError("There are no changes to commit")
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "Poor Man's Code")
        env.setdefault("GIT_AUTHOR_EMAIL", "pmc@localhost")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        p = subprocess.run(
            ["git", "-C", str(worktree), "commit", "-m", message],
            text=True,
            capture_output=True,
            env=env,
        )
        if p.returncode != 0:
            raise GitError(p.stderr.strip())
        return resolve_commit(worktree, "HEAD")

    def destroy(self, repo: Path, worktree: Path, force: bool = False) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree))
        git(repo, *args, check=False)
        if worktree.exists() and force:
            shutil.rmtree(worktree, ignore_errors=True)
        git(repo, "worktree", "prune", check=False)
