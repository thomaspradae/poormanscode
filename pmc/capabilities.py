from __future__ import annotations

import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import Candidate, Job

PROBES = {
    "python": ("python", "python3"),
    "node": ("node",),
    "npm": ("npm",),
    "browser": ("chromium", "chromium-browser", "google-chrome", "firefox"),
    "rustc": ("rustc",),
    "cargo": ("cargo",),
    "dotnet": ("dotnet",),
    "docker": ("docker", "podman"),
    "unity-editor": ("unity-editor", "Unity"),
    "git": ("git",),
}


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    resource: str
    capabilities: frozenset[str]
    details: dict[str, Any]


def probe_local_capabilities() -> CapabilitySnapshot:
    found: set[str] = {"filesystem", "shell"}
    details: dict[str, Any] = {"host": socket.gethostname(), "executables": {}}
    for capability, commands in PROBES.items():
        path = next(
            (shutil.which(command) for command in commands if shutil.which(command)),
            None,
        )
        if path:
            found.add(capability)
            details["executables"][capability] = path
    if "npm" in found:
        found.update({"scaffolder:create-next-app", "scaffolder:vite"})
    if "cargo" in found:
        found.add("scaffolder:cargo-init")
    if "dotnet" in found:
        found.add("scaffolder:dotnet-new")
    if "python" in found:
        found.add("scaffolder:python")
    if "unity-editor" in found:
        found.add("scaffolder:unity")
    return CapabilitySnapshot("controller", frozenset(found), details)


def infer_required_capabilities(request: str, *, skeletal: bool = False) -> list[str]:
    text = request.lower()
    required: set[str] = set()
    if "unity" in text:
        required.update(
            {"unity-editor", "scaffolder:unity"} if skeletal else {"unity-editor"}
        )
    if any(term in text for term in ("next.js", "nextjs", "create-next-app")):
        required.update({"node", "npm"})
        if skeletal:
            required.add("scaffolder:create-next-app")
    elif any(term in text for term in ("vite", "react website", "web app", "website")):
        required.update({"node", "npm"})
        if skeletal:
            required.add("scaffolder:vite")
    if any(term in text for term in ("browser test", "screenshot", "playwright")):
        required.add("browser")
    if any(term in text for term in ("rust", "cargo")):
        required.update({"rustc", "cargo"})
        if skeletal:
            required.add("scaffolder:cargo-init")
    if any(term in text for term in (".net", "dotnet", "c# api", "asp.net")):
        required.add("dotnet")
        if skeletal:
            required.add("scaffolder:dotnet-new")
    if any(term in text for term in ("python", "django", "fastapi", "flask")):
        required.add("python")
        if skeletal:
            required.add("scaffolder:python")
    if "docker" in text or "container" in text:
        required.add("docker")
    return sorted(required)


def repository_is_skeletal(repo: Path) -> bool:
    from .gitops import git

    control = {
        "AGENTS.md",
        "poorman.yaml",
        "README.md",
        "LICENSE",
        ".gitignore",
        ".gitattributes",
    }
    files = {name for name in git(repo, "ls-files").stdout.splitlines() if name}
    return not (files - control)


class CapabilityRegistry:
    def __init__(self, db: Any):
        self.db = db
        self.local = probe_local_capabilities()
        self.db.record_capability_snapshot(
            self.local.resource, sorted(self.local.capabilities), self.local.details
        )

    def candidate_capabilities(self, candidate: Candidate) -> set[str]:
        declared = set(candidate.extra.get("capabilities") or [])
        if candidate.executor in {"bash", "openhands"}:
            declared.update(self.local.capabilities)
        if candidate.executor == "jules":
            declared.update({"github-visible-repository", "network"})
        return declared

    def missing(self, job: Job, candidate: Candidate) -> list[str]:
        required = set(job.constraints.get("required_capabilities") or [])
        return sorted(required - self.candidate_capabilities(candidate))
