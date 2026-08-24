from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
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
    address_space_bytes: int | None = None
    processes: int = 128
    file_bytes: int | None = 512 * 1024**2
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

    def command(
        self,
        worktree: Path,
        command: str,
        network: bool,
        readonly_paths: tuple[Path, ...] = (),
        readonly_bindings: tuple[tuple[Path, Path], ...] = (),
        writable_bindings: tuple[tuple[Path, Path], ...] = (),
    ) -> list[str]:
        raise NotImplementedError

    def run(
        self,
        worktree: Path,
        command: str,
        *,
        env: dict[str, str],
        network: bool,
        limits: SandboxLimits,
        readonly_paths: tuple[Path, ...] = (),
        readonly_bindings: tuple[tuple[Path, Path], ...] = (),
        writable_bindings: tuple[tuple[Path, Path], ...] = (),
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
        args = self.command(
            worktree,
            command,
            network,
            readonly_paths,
            readonly_bindings,
            writable_bindings,
        )
        if shutil.which("prlimit"):
            limit_args = ["prlimit", f"--cpu={limits.cpu_seconds}"]
            address_space = (
                limits.memory_bytes
                if limits.address_space_bytes is None
                else limits.address_space_bytes
            )
            if address_space:
                limit_args.append(f"--as={address_space}")
            if limits.file_bytes is not None:
                limit_args.append(f"--fsize={limits.file_bytes}")
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

    def command(
        self,
        worktree: Path,
        command: str,
        network: bool,
        readonly_paths=(),
        readonly_bindings=(),
        writable_bindings=(),
    ) -> list[str]:
        return ["sudo", "-n", "-u", "pmc-worker", "/bin/bash", "-lc", command]


class ContainerSandbox(Sandbox):
    name = "bwrap"
    network_policies = frozenset({"none", "full"})

    def command(
        self,
        worktree: Path,
        command: str,
        network: bool,
        readonly_paths=(),
        readonly_bindings=(),
        writable_bindings=(),
    ) -> list[str]:
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
            "--dir",
            "/tmp/pmc-home",
            "--chdir",
            "/workspace",
        ]
        for path in (
            "/etc/passwd",
            "/etc/group",
            "/etc/nsswitch.conf",
            "/etc/hosts",
            "/etc/ssl",
            "/etc/machine-id",
            "/etc/os-release",
            "/sys",
        ):
            if Path(path).exists():
                args.extend(["--ro-bind", path, path])
        for path in readonly_paths:
            resolved = path.resolve()
            if not resolved.exists():
                raise RuntimeError(f"sandbox readonly path does not exist: {resolved}")
            args.extend(["--ro-bind", str(resolved), str(resolved)])
        made: set[Path] = set()
        for source, target in readonly_bindings:
            source = source.resolve()
            if not source.exists():
                continue
            target = Path(target)
            chain = [p for p in target.parents if str(p).startswith("/tmp/pmc-home")]
            for parent in reversed(chain):
                if parent not in made and parent != Path("/tmp/pmc-home"):
                    args.extend(["--dir", str(parent)])
                    made.add(parent)
            args.extend(["--ro-bind", str(source), str(target)])
        for source, target in writable_bindings:
            source = source.resolve()
            if not source.exists():
                continue
            target = Path(target)
            chain = [p for p in target.parents if str(p).startswith("/tmp/pmc-home")]
            for parent in reversed(chain):
                if parent not in made and parent != Path("/tmp/pmc-home"):
                    args.extend(["--dir", str(parent)])
                    made.add(parent)
            args.extend(["--bind", str(source), str(target)])
        if not network:
            args.append("--unshare-net")
        return [*args, "/bin/bash", "-lc", command]


class RemoteLxdSandbox(Sandbox):
    """Run untrusted commands in a credential-free LXD container over SSH."""

    name = "remote-lxd"
    network_policies = frozenset({"none"})

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.host = str(config.get("remote_host", ""))
        self.instance = str(config.get("remote_instance", ""))
        self.seed_node_modules = str(config.get("remote_seed_node_modules", ""))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.host):
            raise RuntimeError("remote-lxd requires a safe remote_host")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.instance):
            raise RuntimeError("remote-lxd requires a safe remote_instance")
        if self.seed_node_modules and not self.seed_node_modules.startswith(
            "/opt/pmc-node-cache/"
        ):
            raise RuntimeError("remote seed must be under /opt/pmc-node-cache")

    def _workspace(self, worktree: Path) -> str:
        suffix = re.sub(r"[^A-Za-z0-9_.-]", "-", worktree.name)
        return f"/workspace/{suffix}"

    def _ssh(self, remote_command: str) -> list[str]:
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            self.host,
            remote_command,
        ]

    def _sync_to_remote(self, worktree: Path) -> None:
        remote = self._workspace(worktree)
        prepare = (
            f"lxc exec {shlex.quote(self.instance)} -- mkdir -p {shlex.quote(remote)}"
        )
        subprocess.run(self._ssh(prepare), check=True, capture_output=True, text=True)
        tar = subprocess.Popen(
            [
                "tar",
                "-C",
                str(worktree),
                "--exclude=.git",
                "--exclude=node_modules",
                "--exclude=.next",
                "-cf",
                "-",
                ".",
            ],
            stdout=subprocess.PIPE,
        )
        extract = (
            f"lxc exec {shlex.quote(self.instance)} -- "
            f"tar -C {shlex.quote(remote)} -xf -"
        )
        receive = subprocess.run(
            self._ssh(extract), stdin=tar.stdout, capture_output=True, check=False
        )
        assert tar.stdout is not None
        tar.stdout.close()
        tar_code = tar.wait()
        if tar_code or receive.returncode:
            raise RuntimeError("failed to synchronize worktree to remote sandbox")
        if self.seed_node_modules:
            seed = (
                f"lxc exec {shlex.quote(self.instance)} -- /bin/bash -lc "
                + shlex.quote(
                    f"test -e {shlex.quote(remote)}/node_modules || "
                    f"cp -a {shlex.quote(self.seed_node_modules)} "
                    f"{shlex.quote(remote)}/node_modules"
                )
            )
            subprocess.run(
                self._ssh(seed), check=True, capture_output=True, text=True
            )

    def _sync_from_remote(self, worktree: Path) -> None:
        remote = self._workspace(worktree)
        archive = (
            f"lxc exec {shlex.quote(self.instance)} -- tar -C {shlex.quote(remote)} "
            "--exclude=.git --exclude=node_modules --exclude=.next -cf - ."
        )
        with tempfile.TemporaryDirectory(prefix="pmc-remote-sync-") as temp:
            receive = subprocess.Popen(self._ssh(archive), stdout=subprocess.PIPE)
            extract = subprocess.run(
                ["tar", "-C", temp, "-xf", "-"],
                stdin=receive.stdout,
                capture_output=True,
                check=False,
            )
            assert receive.stdout is not None
            receive.stdout.close()
            receive_code = receive.wait()
            if receive_code or extract.returncode:
                raise RuntimeError("failed to synchronize remote sandbox result")
            for child in worktree.iterdir():
                if child.name in {".git", "node_modules", ".next"}:
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            for child in Path(temp).iterdir():
                shutil.move(str(child), worktree / child.name)

    def command(
        self,
        worktree: Path,
        command: str,
        network: bool,
        readonly_paths=(),
        readonly_bindings=(),
        writable_bindings=(),
    ) -> list[str]:
        if network:
            raise RuntimeError("remote-lxd worker network must remain disabled")
        remote = self._workspace(worktree)
        encoded = base64.b64encode(command.encode()).decode("ascii")
        invoke = (
            f"lxc exec {shlex.quote(self.instance)} "
            "--env PATH=/usr/local/bin:/usr/bin:/bin "
            "--env HOME=/tmp/pmc-home "
            f"--cwd {shlex.quote(remote)} -- /bin/bash -lc "
            f"{shlex.quote(f'echo {encoded} | base64 -d | /bin/bash')}"
        )
        return self._ssh(invoke)

    def run(
        self,
        worktree: Path,
        command: str,
        *,
        env: dict[str, str],
        network: bool,
        limits: SandboxLimits,
        readonly_paths=(),
        readonly_bindings=(),
        writable_bindings=(),
    ) -> subprocess.CompletedProcess[str]:
        if network or readonly_paths or readonly_bindings or writable_bindings:
            return subprocess.CompletedProcess(
                [], 78, "", "PMC_POLICY_FAILURE: unsupported remote-lxd policy\n"
            )
        self._sync_to_remote(worktree)
        limited = (
            f"ulimit -t {limits.cpu_seconds}; ulimit -f "
            f"{max(1, (limits.file_bytes or 512 * 1024**2) // 512)}; "
            f"timeout --signal=TERM --kill-after=2 {limits.wall_seconds}s "
            f"/bin/bash -lc {shlex.quote(command)}"
        )
        args = self.command(worktree, limited, False)
        proc = subprocess.run(
            args,
            text=True,
            capture_output=True,
            env=scrubbed_environment(),
            timeout=limits.wall_seconds + 15,
            check=False,
        )
        self._sync_from_remote(worktree)
        after_bytes, after_files = workspace_usage(worktree)
        if after_bytes > limits.workspace_bytes or after_files > limits.workspace_files:
            return subprocess.CompletedProcess(
                args,
                75,
                proc.stdout,
                proc.stderr
                + f"\nPMC_RESOURCE_LIMIT:WORKSPACE_LIMIT bytes={after_bytes} files={after_files}\n",
            )
        return proc


class GuardedSandbox(Sandbox):
    name = "guarded"
    network_policies = frozenset({"full"})

    def command(
        self,
        worktree: Path,
        command: str,
        network: bool,
        readonly_paths=(),
        readonly_bindings=(),
        writable_bindings=(),
    ) -> list[str]:
        return ["/bin/bash", "-lc", command]


def build_sandbox(name: str, config: dict[str, Any] | None = None) -> Sandbox:
    if name == "bwrap":
        return ContainerSandbox()
    if name == "restricted-user":
        return RestrictedUserSandbox()
    if name in {"none", "guarded"}:
        return GuardedSandbox()
    if name in {"remote", "remote-lxd"}:
        return RemoteLxdSandbox(config)
    raise RuntimeError(f"unknown sandbox mode: {name}")
