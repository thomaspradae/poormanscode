from pathlib import Path

import pytest

from pmc.capabilities import probe_local_capabilities
from pmc.toolchains import UnityToolchain, UnityToolchainError


def executable(tmp_path: Path) -> Path:
    editor = tmp_path / "Unity Editor" / "Unity"
    editor.parent.mkdir()
    editor.write_text("#!/bin/sh\nexit 0\n")
    editor.chmod(0o755)
    return editor


def test_registered_editor_adds_unity_capabilities(tmp_path: Path):
    editor = executable(tmp_path)
    snapshot = probe_local_capabilities(
        {"unity": {"editor_path": str(editor), "version": "6000.0.1f1"}}
    )
    assert {
        "unity-editor",
        "unity-batchmode",
        "scaffolder:unity",
    } <= snapshot.capabilities
    assert "unity-version:6000.0.1f1" in snapshot.capabilities


def test_unity_commands_use_native_batchmode_and_test_runner(tmp_path: Path):
    unity = UnityToolchain.from_config({"editor_path": str(executable(tmp_path))})
    commands = unity.verification_commands(
        {"unity": {"editmode_tests": True, "playmode_tests": True}}
    )
    assert "-batchmode" in commands["unity_compile"]
    assert "-runTests" in commands["unity_editmode"]
    assert "EditMode" in commands["unity_editmode"]
    assert "PlayMode" in commands["unity_playmode"]


def test_bootstrap_refuses_established_repository(tmp_path: Path):
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "source.cs").write_text("class Source {}\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.cs"], check=True)
    unity = UnityToolchain.from_config({"editor_path": str(executable(tmp_path))})
    with pytest.raises(UnityToolchainError, match="not skeletal"):
        unity.bootstrap(tmp_path)
