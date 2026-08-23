from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def scrubbed_environment() -> dict[str, str]:
    clean = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if not any(marker in upper for marker in
                   ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY", "SSH_AUTH_SOCK")):
            clean[key] = value
    return clean


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    wall_seconds: int = 180
    cpu_seconds: int = 120
    memory_bytes: int = 2 * 1024**3
    processes: int = 128
    file_bytes: int = 512 * 1024**2


def resource_snapshot(path: Path) -> dict[str, Any]:
    disk = shutil.disk_usage(path)
    pages = os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else 0
    page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 0
    gpu = shutil.which("nvidia-smi")
    gpu_info: list[str] = []
    if gpu:
        proc = subprocess.run([gpu, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader"],
                              text=True, capture_output=True, timeout=5, check=False)
        if proc.returncode == 0:
            gpu_info = proc.stdout.splitlines()
    return {"cpu_count": os.cpu_count(), "memory_bytes": pages * page_size,
            "disk_free_bytes": disk.free, "gpu": gpu_info}


class Sandbox:
    name = "base"

    def command(self, worktree: Path, command: str, network: bool) -> list[str]:
        raise NotImplementedError

    def run(self, worktree: Path, command: str, *, env: dict[str, str],
            network: bool, limits: SandboxLimits) -> subprocess.CompletedProcess[str]:
        args = self.command(worktree, command, network)
        if shutil.which("prlimit"):
            args = ["prlimit", f"--cpu={limits.cpu_seconds}", f"--as={limits.memory_bytes}",
                    f"--fsize={limits.file_bytes}", "--", *args]
        if shutil.which("setsid"):
            args = ["setsid", *args]
        if shutil.which("timeout"):
            args = ["timeout", "--signal=TERM", "--kill-after=5", str(limits.wall_seconds), *args]
        return subprocess.run(args, cwd=worktree, text=True, capture_output=True, env=env,
                              timeout=limits.wall_seconds + 10, check=False)


class RestrictedUserSandbox(Sandbox):
    name = "restricted-user"

    def command(self, worktree: Path, command: str, network: bool) -> list[str]:
        return ["sudo", "-n", "-u", "pmc-worker", "/bin/bash", "-lc", command]


class ContainerSandbox(Sandbox):
    name = "bwrap"

    def command(self, worktree: Path, command: str, network: bool) -> list[str]:
        if not shutil.which("bwrap"):
            raise RuntimeError("bubblewrap is not installed")
        args = ["bwrap", "--die-with-parent", "--new-session", "--ro-bind", "/", "/",
                "--bind", str(worktree), str(worktree), "--dev", "/dev", "--proc", "/proc",
                "--tmpfs", "/tmp", "--chdir", str(worktree)]
        if not network:
            args.append("--unshare-net")
        return [*args, "/bin/bash", "-lc", command]


class RemoteSandbox(Sandbox):
    name = "remote"

    def command(self, worktree: Path, command: str, network: bool) -> list[str]:
        raise RuntimeError("remote sandboxes are executor-managed")


class GuardedSandbox(Sandbox):
    name = "guarded"

    def command(self, worktree: Path, command: str, network: bool) -> list[str]:
        return ["/bin/bash", "-lc", command]


def build_sandbox(name: str) -> Sandbox:
    if name == "bwrap":
        return ContainerSandbox()
    if name == "restricted-user":
        return RestrictedUserSandbox()
    if name in {"none", "guarded"}:
        return GuardedSandbox()
    if name == "remote":
        return RemoteSandbox()
    raise RuntimeError(f"unknown sandbox mode: {name}")
