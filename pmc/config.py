from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .domain import Candidate


DEFAULT_CONFIG = Path("~/.config/poormans-code/config.toml").expanduser()


@dataclass(slots=True)
class PMCConfig:
    db_path: Path
    runs_dir: Path
    worktrees_dir: Path
    exploration_rate: float = 0.20
    min_samples_per_candidate: int = 5
    max_attempts: int = 3
    same_candidate_retries: int = 2
    review_enabled: bool = False
    lease_ttl_seconds: int = 300
    verifier_sandbox: str = "guarded"
    artifact_max_bytes: int = 512 * 1024**2
    candidates: list[Candidate] = None  # type: ignore[assignment]


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def load_config(path: Path | None = None) -> PMCConfig:
    path = path or Path(os.getenv("PMC_CONFIG", str(DEFAULT_CONFIG))).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"PMC config not found: {path}. Copy examples/config.toml there first."
        )
    raw = tomllib.loads(path.read_text())
    core = raw.get("pmc", {})
    candidates = [Candidate.from_mapping(x) for x in raw.get("candidates", [])]
    cfg = PMCConfig(
        db_path=_expand(core.get("db_path", "~/.local/share/poormans-code/pmc.db")),
        runs_dir=_expand(core.get("runs_dir", "~/.local/share/poormans-code/runs")),
        worktrees_dir=_expand(
            core.get("worktrees_dir", "~/.local/share/poormans-code/worktrees")
        ),
        exploration_rate=float(core.get("exploration_rate", 0.20)),
        min_samples_per_candidate=int(core.get("min_samples_per_candidate", 5)),
        max_attempts=int(core.get("max_attempts", 3)),
        same_candidate_retries=int(core.get("same_candidate_retries", 2)),
        review_enabled=bool(core.get("review_enabled", False)),
        lease_ttl_seconds=int(core.get("lease_ttl_seconds", 300)),
        verifier_sandbox=str(core.get("verifier_sandbox", "guarded")),
        artifact_max_bytes=int(core.get("artifact_max_bytes", 512 * 1024**2)),
        candidates=candidates,
    )
    for p in (cfg.db_path.parent, cfg.runs_dir, cfg.worktrees_dir):
        p.mkdir(parents=True, exist_ok=True)
    return cfg


def load_repo_config(repo: Path) -> dict[str, Any]:
    path = repo / "poorman.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_repo_config_at(repo: Path, revision: str) -> dict[str, Any]:
    """Load acceptance policy from PMC's immutable Git baseline."""
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:poorman.yaml"],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return {}
    data = yaml.safe_load(proc.stdout) or {}
    if not isinstance(data, dict):
        raise TypeError("baseline poorman.yaml must contain a YAML mapping")
    return data


def read_agents(repo: Path, limit: int = 20_000) -> str:
    path = repo / "AGENTS.md"
    if not path.exists():
        return ""
    return path.read_text(errors="replace")[:limit]
