from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import read_agents
from .gitops import git
from .versioning import CONTEXT_BUILDER_VERSION, stable_hash


@dataclass(frozen=True, slots=True)
class ContextBundle:
    content: str
    manifest: dict[str, object]
    content_hash: str


def _terms(text: str) -> set[str]:
    return {x.lower() for x in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,}", text)}


def build_context_bundle(
    repo: Path, request: str, *, baseline: str | None = None, limit: int = 24_000
) -> ContextBundle:
    """Build bounded, reproducible repository context without sending the whole repo."""
    files = [p for p in git(repo, "ls-files").stdout.splitlines() if p]
    terms = _terms(request)
    ranked: list[tuple[int, str]] = []
    for name in files:
        path_terms = _terms(name)
        score = len(terms & path_terms) * 10
        if name.startswith(("src/", "lib/", "app/", "pmc/")):
            score += 1
        ranked.append((score, name))
    ranked.sort(key=lambda item: (-item[0], item[1]))

    sections = ["REPOSITORY MAP:\n" + "\n".join(files[:400])]
    agents = read_agents(repo)
    if agents:
        sections.append("REPOSITORY INSTRUCTIONS:\n" + agents)

    snippets: list[str] = []
    for score, name in ranked:
        if score <= 0 or len(snippets) >= 8:
            break
        path = repo / name
        try:
            if path.stat().st_size > 100_000:
                continue
            body = path.read_text(errors="replace")[:4_000]
        except OSError:
            continue
        snippets.append(f"--- {name} ---\n{body}")
    if snippets:
        sections.append("LIKELY RELEVANT FILE EXCERPTS:\n" + "\n\n".join(snippets))
    dependency_names = {
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "Cargo.toml",
        "Packages/manifest.json",
        "Packages/packages-lock.json",
    }
    dependencies = [name for name in files if name in dependency_names]
    if dependencies:
        dep_text = []
        for name in dependencies:
            dep_text.append(
                f"--- {name} ---\n" + (repo / name).read_text(errors="replace")[:4000]
            )
        sections.append("DEPENDENCY MANIFESTS:\n" + "\n\n".join(dep_text))
    previous_diff = ""
    if baseline:
        previous_diff = git(repo, "diff", "--unified=1", baseline, "--").stdout[:12_000]
        if previous_diff:
            sections.append("CURRENT/PREVIOUS ATTEMPT DIFF:\n" + previous_diff)
    content = "\n\n".join(sections)[:limit]
    manifest = {
        "version": CONTEXT_BUILDER_VERSION,
        "tracked_files": files[:400],
        "excerpt_files": [name for score, name in ranked[:8] if score > 0],
        "dependency_files": dependencies,
        "previous_diff_hash": stable_hash(previous_diff) if previous_diff else None,
        "limit": limit,
    }
    return ContextBundle(
        content, manifest, stable_hash({"content": content, "manifest": manifest})
    )


def build_context_packet(repo: Path, request: str, *, limit: int = 24_000) -> str:
    return build_context_bundle(repo, request, limit=limit).content
