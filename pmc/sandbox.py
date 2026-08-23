from __future__ import annotations

import os
import signal
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def scrubbed_environment() -> dict[str, str]:
    clean = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if not any(
            marker in upper
            for marker in (
                "TOKEN",
                "SECRET",
                "PASSWORD",
                "API_KEY",
                "PRIVATE_KEY",
                "SSH_AUTH_SOCK",
            )
        ):
            clean[key] = value
    return clean


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    wall_seconds: int = 180
    cpu_seconds: int = 120
    memory_bytes: int = 2 * 1024**3
    processes: int = 128
    file_bytes: int = 512 * 1024**2
    workspace_bytes: int = 2 * 1024**3
    workspace_files: int = 50_000
    artifact_bytes: int = 512 * 1024**2


def workspace_usage(path: Path) -> tuple[int, int]:
    total = files = 0
    for root, _, names in os.walk(path):
        for name in names:
            files += 1
            try:
                total += (Path(root) / name).stat().st_size
            except FileNotFoundError:
                pass
    return total, files


def resource_snapshot(path: Path) -> dict[str, Any]:
    disk = shutil.disk_usage(path)
    pages = os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else 0
    page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 0
    gpu = shutil.which("nvidia-smi")
    gpu_info: list[str] = []
    if gpu:
        proc = subprocess.run(
            [gpu, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            gpu_info = proc.stdout.splitlines()
    return {
        "cpu_count": os.cpu_count(),
        "memory_bytes": pages * page_size,
        "disk_free_bytes": disk.free,
        "gpu": gpu_info,
    }


class Sandbox:
    name = "base"
    network_policies: frozenset[str] = frozenset()

    def supports_network_policy(self, policy: str) -> bool:
        return policy in self.network_policies

    def command(self, worktree: Path, command: str, network: bool) -> list[str]:
        raise NotImplementedError

    def run(
        self,
        worktree: Path,
        command: str,
        *,
        env: dict[str, str],
        network: bool,
        limits: SandboxLimits,
    ) -> subprocess.CompletedProcess[str]:
        requested_policy = "full" if network else "none"
        if not self.supports_network_policy(requested_policy):
            return subprocess.CompletedProcess(
                [],
                78,
                "",
                f"PMC_POLICY_FAILURE: sandbox {self.name} cannot enforce "
                f"network_policy={requested_policy}\n",
            )
        before_bytes, before_files = workspace_usage(worktree)
        if (
            before_bytes > limits.workspace_bytes
            or before_files > limits.workspace_files
        ):
            return subprocess.CompletedProcess(
                [], 75, "", "PMC_RESOURCE_LIMIT:WORKSPACE_LIMIT\n"
            )
        args = self.command(worktree, command, network)
        if shutil.which("prlimit"):
            limit_args = [
                "prlimit",
                f"--cpu={limits.cpu_seconds}",
                f"--as={limits.memory_bytes}",
                f"--fsize={limits.file_bytes}",
            ]
            if self.name == "restricted-user":
                limit_args.append(f"--nproc={limits.processes}")
            args = [*limit_args, "--", *args]
        if self.name == "bwrap" and shutil.which("systemd-run"):
            args = [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--property",
                f"TasksMax={limits.processes}",
                "--property",
                f"MemoryMax={limits.memory_bytes}",
                "--",
                *args,
            ]
        runner_env = dict(env)
        if self.name == "bwrap" and args and args[0] == "systemd-run":
            runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            runner_env.setdefault("XDG_RUNTIME_DIR", runtime)
            runner_env.setdefault(
                "DBUS_SESSION_BUS_ADDRESS",
                os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus"),
            )
        proc = subprocess.Popen(
            args,
            cwd=worktree,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=runner_env,
            start_new_session=True,
        )
        monitor_stop = threading.Event()
        violation: list[tuple[int, int]] = []

        def monitor_workspace() -> None:
            while not monitor_stop.wait(0.05):
                current_bytes, current_files = workspace_usage(worktree)
                if (
                    current_bytes > limits.workspace_bytes
                    or current_files > limits.workspace_files
                ):
                    violation.append((current_bytes, current_files))
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    return

        monitor = threading.Thread(
            target=monitor_workspace, daemon=True, name=f"pmc-disk-{proc.pid}"
        )
        monitor.start()
        try:
            stdout, stderr = proc.communicate(timeout=limits.wall_seconds)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
            return subprocess.CompletedProcess(
                args, 124, stdout, stderr + "\nPMC_RESOURCE_LIMIT:TIMEOUT\n"
            )
        finally:
            monitor_stop.set()
            monitor.join(timeout=1)
        if violation:
            after_bytes, after_files = violation[-1]
            return subprocess.CompletedProcess(
                args,
                75,
                stdout,
                stderr
                + f"\nPMC_RESOURCE_LIMIT:WORKSPACE_LIMIT bytes={after_bytes} files={after_files}\n",
            )
        after_bytes, after_files = workspace_usage(worktree)
        if after_bytes > limits.workspace_bytes or after_files > limits.workspace_files:
            return subprocess.CompletedProcess(
                args,
                75,
                stdout,
                stderr
                + f"\nPMC_RESOURCE_LIMIT:WORKSPACE_LIMIT bytes={after_bytes} files={after_files}\n",
            )
        return subprocess.CompletedProcess(args, code, stdout, stderr)


class RestrictedUserSandbox(Sandbox):
    name = "restricted-user"
    network_policies = frozenset({"full"})

    def command(self, worktree: Path, command: str, network: bool) -> list[str]:
        return ["sudo", "-n", "-u", "pmc-worker", "/bin/bash", "-lc", command]


class ContainerSandbox(Sandbox):
    name = "bwrap"
    network_policies = frozenset({"none", "full"})

    def command(self, worktree: Path, command: str, network: bool) -> list[str]:
        if not shutil.which("bwrap"):
            raise RuntimeError("bubblewrap is not installed")
        args = [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
            "--setenv",
            "HOME",
            "/tmp/pmc-home",
            "--dir",
            "/etc",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib",
            "/lib64",
            "--bind",
            str(worktree),
            "/workspace",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--chdir",
            "/workspace",
        ]
        for path in (
            "/etc/passwd",
            "/etc/group",
            "/etc/nsswitch.conf",
            "/etc/hosts",
            "/etc/ssl",
        ):
            if Path(path).exists():
                args.extend(["--ro-bind", path, path])
        if not network:
            args.append("--unshare-net")
        return [*args, "/bin/bash", "-lc", command]


class RemoteSandbox(Sandbox):
    name = "remote"

    def command(self, worktree: Path, command: str, network: bool) -> list[str]:
        raise RuntimeError("remote sandboxes are executor-managed")


class GuardedSandbox(Sandbox):
    name = "guarded"
    network_policies = frozenset({"full"})

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
