from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 8
JOB_CONTRACT_VERSION = "2"
PROMPT_PROFILE_VERSION = "builder-v5-intelligence"
CONTEXT_BUILDER_VERSION = "context-v3"
SCHEDULER_POLICY_VERSION = "contextual-thompson-v4"
VERIFIER_VERSION = "deterministic-v2"
TOOLCHAIN_PROFILE_VERSION = "toolchains-v1"


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pmc_git_sha() -> str:
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"
