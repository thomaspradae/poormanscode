from __future__ import annotations

import fnmatch
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .domain import CommandResult, Job, VerificationResult
from .gitops import git, intent_to_add_untracked
from .sandbox import SandboxLimits, build_sandbox, scrubbed_environment


SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{12,}['\"]"
    ),
]


def _run(
    name: str,
    command: str,
    cwd: Path,
    timeout: int,
    sandbox_name: str = "guarded",
    readonly_paths: tuple[Path, ...] = (),
    memory_bytes: int = 2 * 1024**3,
    readonly_bindings: tuple[tuple[Path, Path], ...] = (),
    force_network: bool = False,
    writable_bindings: tuple[tuple[Path, Path], ...] = (),
    file_bytes: int | None = 512 * 1024**2,
    address_space_bytes: int | None = None,
    processes: int = 128,
) -> CommandResult:
    started = time.monotonic()
    try:
        sandbox = build_sandbox(sandbox_name)
        # Verifiers do not receive secrets. Use no-network when enforceable;
        # otherwise explicitly run with full network rather than overclaiming.
        network = force_network or not sandbox.supports_network_policy("none")
        p = sandbox.run(
            cwd,
            command,
            env=scrubbed_environment(),
            network=network,
            limits=SandboxLimits(
                wall_seconds=timeout,
                cpu_seconds=max(1, timeout - 5),
                memory_bytes=memory_bytes,
                file_bytes=file_bytes,
                address_space_bytes=address_space_bytes,
                processes=processes,
            ),
            readonly_paths=readonly_paths,
            readonly_bindings=readonly_bindings,
            writable_bindings=writable_bindings,
        )
        return CommandResult(
            name, command, p.returncode, time.monotonic() - started, p.stdout, p.stderr
        )
    except subprocess.TimeoutExpired as exc:
        out = (
            exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        )
        err = (
            exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        )
        return CommandResult(
            name, command, 124, time.monotonic() - started, out, err + "\nTIMEOUT"
        )


def _matches_any(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    for p in patterns:
        if fnmatch.fnmatch(normalized, p) or fnmatch.fnmatch(
            normalized, p.rstrip("/**")
        ):
            return True
    return False


def _patch_lines(worktree: Path, baseline: str) -> int:
    out = git(worktree, "diff", "--numstat", baseline, "--").stdout
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            for x in parts[:2]:
                if x.isdigit():
                    total += int(x)
    return total


def _added_diff(worktree: Path, baseline: str) -> str:
    return git(worktree, "diff", "--unified=0", baseline, "--").stdout


def _secret_scan(diff: str) -> list[str]:
    findings = []
    added = "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    for p in SECRET_PATTERNS:
        m = p.search(added)
        if m:
            findings.append(f"possible secret in added content: {m.group(0)[:80]}")
    return findings


def verify(
    job: Job, worktree: Path, repo_cfg: dict[str, Any], sandbox_name: str = "guarded"
) -> VerificationResult:
    baseline = job.baseline_commit
    if not baseline:
        raise RuntimeError("job has no baseline commit")
    intent_to_add_untracked(worktree, list(repo_cfg.get("verification_ignore", [])))
    timeout = int(repo_cfg.get("timeout_seconds", 600))
    commands: list[CommandResult] = []
    profile_commands: dict[str, str] = {}
    readonly_paths: tuple[Path, ...] = ()
    verifier_memory = 2 * 1024**3
    readonly_bindings: tuple[tuple[Path, Path], ...] = ()
    writable_bindings: tuple[tuple[Path, Path], ...] = ()
    profile_network = False
    profile_file_bytes: int | None = 512 * 1024**2
    profile_address_space: int | None = None
    profile_processes = 128
    if repo_cfg.get("toolchain") == "unity":
        from .toolchains import UnityToolchain

        toolchain_cfg = dict(repo_cfg.get("unity_toolchain", {}))
        unity = UnityToolchain.from_config(toolchain_cfg)
        profile_commands = unity.verification_commands(repo_cfg)
        readonly_paths = (unity.editor.parent,)
        verifier_memory = (
            int(repo_cfg.get("unity", {}).get("memory_mb", 6144)) * 1024**2
        )
        readonly_bindings = unity.sandbox_ipc_bindings()
        profile_network = bool(
            repo_cfg.get("unity", {}).get("network_for_license", False)
        )
        max_single = int(repo_cfg.get("unity", {}).get("max_single_file_mb", 0))
        profile_file_bytes = max_single * 1024**2 if max_single else None
        profile_address_space = 0
        profile_processes = int(repo_cfg.get("unity", {}).get("process_limit", 512))
    for name in ("test", "lint", "typecheck", "build", "hidden_test"):
        command = repo_cfg.get(name)
        if command:
            commands.append(_run(name, str(command), worktree, timeout, sandbox_name))
    for name, command in profile_commands.items():
        commands.append(
            _run(
                name,
                command,
                worktree,
                timeout,
                sandbox_name,
                readonly_paths,
                verifier_memory,
                readonly_bindings,
                profile_network,
                writable_bindings,
                profile_file_bytes,
                profile_address_space,
                profile_processes,
            )
        )
    changed = [
        x
        for x in git(
            worktree, "diff", "--name-only", baseline, "--"
        ).stdout.splitlines()
        if x
    ]
    patch_lines = _patch_lines(worktree, baseline)
    findings: list[str] = []

    max_files = int(
        job.constraints.get("max_files_changed", repo_cfg.get("max_files_changed", 10))
    )
    max_lines = int(
        job.constraints.get("max_patch_lines", repo_cfg.get("max_patch_lines", 500))
    )
    scope_ok = len(changed) <= max_files and patch_lines <= max_lines
    if len(changed) > max_files:
        findings.append(f"changed {len(changed)} files; budget is {max_files}")
    if patch_lines > max_lines:
        findings.append(f"patch has {patch_lines} changed lines; budget is {max_lines}")

    allowed = job.constraints.get("allowed_paths")
    if allowed:
        outside = [p for p in changed if not _matches_any(p, list(allowed))]
        if outside:
            scope_ok = False
            findings.append("changes outside allowed_paths: " + ", ".join(outside))

    protected = list(repo_cfg.get("protected", [])) + list(
        job.constraints.get("protected", [])
    )
    protected_hits = [p for p in changed if _matches_any(p, protected)]
    protected_ok = not protected_hits
    if protected_hits:
        findings.append("protected paths changed: " + ", ".join(protected_hits))

    no_new_deps = bool(
        job.constraints.get(
            "no_new_dependencies", repo_cfg.get("no_new_dependencies", False)
        )
    )
    dependency_files = set(repo_cfg.get("dependency_files", []))
    dep_hits = sorted(dependency_files.intersection(changed)) if no_new_deps else []
    dependencies_ok = not dep_hits
    if dep_hits:
        findings.append(
            "dependency manifests changed while no_new_dependencies=true: "
            + ", ".join(dep_hits)
        )

    diff = _added_diff(worktree, baseline)
    secret_findings = _secret_scan(diff)
    findings.extend(secret_findings)
    secret_ok = not secret_findings

    commands_ok = all(c.ok for c in commands)
    if not changed:
        findings.append("executor produced no code changes")
        scope_ok = False
    ok = commands_ok and scope_ok and protected_ok and secret_ok and dependencies_ok
    return VerificationResult(
        ok=ok,
        commands=commands,
        changed_files=changed,
        patch_lines=patch_lines,
        scope_ok=scope_ok,
        secret_scan_ok=secret_ok,
        protected_paths_ok=protected_ok,
        dependencies_ok=dependencies_ok,
        findings=findings,
    )
