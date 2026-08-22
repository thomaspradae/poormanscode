from __future__ import annotations

import re
from pathlib import Path

from .config import read_agents
from .gitops import git


def _terms(text: str) -> set[str]:
    return {x.lower() for x in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,}", text)}


def build_context_packet(repo: Path, request: str, *, limit: int = 24_000) -> str:
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
    return "\n\n".join(sections)[:limit]
