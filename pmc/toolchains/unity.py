from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..capabilities import repository_is_skeletal
from ..sandbox import scrubbed_environment


class UnityToolchainError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UnityToolchain:
    editor: Path
    version: str | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> UnityToolchain:
        value = config.get("editor_path")
        if not value:
            raise UnityToolchainError("Unity editor_path is not configured")
        editor = Path(str(value)).expanduser().resolve()
        if not editor.is_file() or not os.access(editor, os.X_OK):
            raise UnityToolchainError(f"Unity Editor is not executable: {editor}")
        return cls(editor, str(config["version"]) if config.get("version") else None)

    def bootstrap(
        self, repo: Path, *, timeout: int = 1800
    ) -> subprocess.CompletedProcess[str]:
        if not repository_is_skeletal(repo):
            raise UnityToolchainError(
                "refusing Unity bootstrap: repository is not skeletal"
            )
        log = repo / ".pmc-unity-bootstrap.log"
        command = [
            str(self.editor),
            "-batchmode",
            "-nographics",
            "-quit",
            "-createProject",
            str(repo),
            "-logFile",
            str(log),
        ]
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=scrubbed_environment(),
            check=False,
        )
        if result.returncode:
            tail = (
                log.read_text(errors="replace")[-5000:]
                if log.exists()
                else result.stderr[-5000:]
            )
            raise UnityToolchainError(
                f"Unity project creation failed ({result.returncode}):\n{tail}"
            )
        return result

    def command(self, *arguments: str) -> str:
        return " ".join(
            shlex.quote(x)
            for x in (
                str(self.editor),
                "-batchmode",
                "-nographics",
                "-projectPath",
                ".",
                "-accept-apiupdate",
                *arguments,
            )
        )

    def verification_commands(self, policy: dict[str, Any]) -> dict[str, str]:
        unity = dict(policy.get("unity", {}))
        commands = {"unity_compile": self.command("-quit", "-logFile", "-")}
        if unity.get("editmode_tests", True):
            commands["unity_editmode"] = self.command(
                "-runTests",
                "-testPlatform",
                "EditMode",
                "-testResults",
                "Temp/pmc-editmode-results.xml",
                "-logFile",
                "-",
            )
        if unity.get("playmode_tests", False):
            commands["unity_playmode"] = self.command(
                "-runTests",
                "-testPlatform",
                "PlayMode",
                "-testResults",
                "Temp/pmc-playmode-results.xml",
                "-logFile",
                "-",
            )
        return commands

    def sandbox_ipc_bindings(self) -> tuple[tuple[Path, Path], ...]:
        sockets = []
        for name in (
            "Unity-LicenseClient-t.sock",
            "Unity-LicenseClient-t-notifications.sock",
        ):
            path = Path("/tmp") / name
            if path.exists():
                sockets.append((path, path))
        return tuple(sockets)
