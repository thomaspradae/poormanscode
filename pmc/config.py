from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .domain import Candidate

DEFAULT_CONFIG = Path("~/.config/poormans-code/config.toml").expanduser()
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    research_enabled: bool = False
    research_model: str = "gemini-3-flash-preview"
    research_api_key_env: str = "GEMINI_API_KEY"
    research_max_queries_per_attempt: int = 5
    toolchains: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    candidates: list[Candidate] = None  # type: ignore[assignment]


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def load_secrets_file(path: Path) -> list[str]:
    """Load literal KEY=VALUE entries without shell evaluation or logging values."""
    if not path.exists():
        return []
    info = path.stat()
    if info.st_uid != os.getuid():
        raise PermissionError(f"secrets file must be owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError(f"secrets file must have mode 600: {path}")
    loaded: list[str] = []
    for number, original in enumerate(path.read_text().splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid secrets.env line {number}: expected KEY=VALUE")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(
                f"invalid environment-variable name on secrets.env line {number}"
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name, value)
        loaded.append(name)
    return loaded


def load_config(path: Path | None = None) -> PMCConfig:
    path = path or Path(os.getenv("PMC_CONFIG", str(DEFAULT_CONFIG))).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"PMC config not found: {path}. Copy examples/config.toml there first."
        )
    load_secrets_file(path.parent / "secrets.env")
    raw = tomllib.loads(path.read_text())
    core = raw.get("pmc", {})
    research = raw.get("research", {})
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
        research_enabled=bool(research.get("enabled", False)),
        research_model=str(research.get("model", "gemini-3-flash-preview")),
        research_api_key_env=str(research.get("api_key_env", "GEMINI_API_KEY")),
        research_max_queries_per_attempt=int(
            research.get("max_queries_per_attempt", 5)
        ),
        toolchains={str(k): dict(v) for k, v in raw.get("toolchains", {}).items()},
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
