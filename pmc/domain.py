from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    READY = "READY_FOR_REVIEW"
    RETRY = "RETRY"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ACCEPTED = "ACCEPTED"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    EXECUTOR_FAILED = "EXECUTOR_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"
    REVIEW_FAILED = "REVIEW_FAILED"
    READY = "READY"


@dataclass(slots=True)
class Job:
    id: str
    repo: Path
    request: str
    base_branch: str = "main"
    priority: int = 2
    task_type: str = "UNKNOWN"
    acceptance: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    state: JobState = JobState.QUEUED
    worktree: Path | None = None
    baseline_commit: str | None = None


@dataclass(slots=True)
class Candidate:
    name: str
    executor: str
    version: str = "1"
    provider: str | None = None
    prompt_profile: str = "builder-v2"
    tool_profile: str = "default"
    resource_class: str = "default"
    role: str = "builder"
    enabled: bool = True
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    quota_group: str | None = None
    resource_group: str | None = None
    resource_concurrency: int | None = None
    quota_attempts: int | None = None
    quota_window_seconds: int | None = None
    max_concurrency: int | None = None
    max_turns: int = 20
    sandbox: str = "none"
    network: bool = False
    network_policy: str | None = None
    source: str | None = None
    server_url: str | None = None
    monetary_cost_hint: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_network_policy(self) -> str:
        return self.network_policy or ("full" if self.network else "none")

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "Candidate":
        known = {f.name for f in cls.__dataclass_fields__.values()} - {"extra"}
        base = {k: raw[k] for k in raw if k in known}
        extra = {k: raw[k] for k in raw if k not in known}
        base["extra"] = extra
        return cls(**base)


@dataclass(slots=True)
class ExecutionRequest:
    job: Job
    candidate: Candidate
    worktree: Path
    prompt: str
    attempt_no: int
    feedback: str | None = None
    accounting: Any | None = None


class Outcome(StrEnum):
    SUCCESS = "SUCCESS"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    RATE_LIMIT = "RATE_LIMIT"
    RESOURCE_FAILURE = "RESOURCE_FAILURE"
    EXECUTOR_FAILURE = "EXECUTOR_FAILURE"
    EXECUTOR_CRASH = "EXECUTOR_CRASH"
    TIMEOUT = "TIMEOUT"
    PROTOCOL_FAILURE = "PROTOCOL_FAILURE"
    FORMAT_FAILURE = "FORMAT_FAILURE"
    TEST_FAILURE = "TEST_FAILURE"
    LINT_FAILURE = "LINT_FAILURE"
    BUILD_FAILURE = "BUILD_FAILURE"
    TYPECHECK_FAILURE = "TYPECHECK_FAILURE"
    SCOPE_FAILURE = "SCOPE_FAILURE"
    SECURITY_FAILURE = "SECURITY_FAILURE"
    POLICY_FAILURE = "POLICY_FAILURE"
    REVIEW_FAILURE = "REVIEW_FAILURE"
    HUMAN_REJECT = "HUMAN_REJECT"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class ExecutionResult:
    ok: bool
    summary: str = ""
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    provider_request_id: str | None = None
    raw_metrics: dict[str, Any] = field(default_factory=dict)
    outcome: Outcome | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CommandResult:
    name: str
    command: str
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(slots=True)
class VerificationResult:
    ok: bool
    commands: list[CommandResult]
    changed_files: list[str]
    patch_lines: int
    scope_ok: bool
    secret_scan_ok: bool
    protected_paths_ok: bool
    dependencies_ok: bool
    findings: list[str] = field(default_factory=list)

    def short_failure(self) -> str:
        bits = list(self.findings)
        for cmd in self.commands:
            if not cmd.ok:
                tail = (cmd.stdout + "\n" + cmd.stderr).strip()[-3000:]
                bits.append(f"{cmd.name} failed ({cmd.exit_code}):\n{tail}")
        return "\n\n".join(bits) or "verification failed"


@dataclass(slots=True)
class ReviewResult:
    ok: bool
    verdict: str
    summary: str
    findings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SchedulerDecision:
    candidate: Candidate
    mode: str
    score: float
    reason: str
    eligible: list[str] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)
    selection_probability: float = 1.0
    policy_version: str = "unknown"
    snapshot: dict[str, Any] = field(default_factory=dict)
