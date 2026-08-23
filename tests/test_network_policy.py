import shutil
from pathlib import Path

import pytest

from pmc.sandbox import (
    ContainerSandbox,
    GuardedSandbox,
    RestrictedUserSandbox,
    SandboxLimits,
)


def test_network_capabilities_are_explicit_and_not_overclaimed():
    assert GuardedSandbox().supports_network_policy("full")
    assert not GuardedSandbox().supports_network_policy("none")
    assert not RestrictedUserSandbox().supports_network_policy("none")
    assert ContainerSandbox().supports_network_policy("none")
    assert not ContainerSandbox().supports_network_policy("restricted")


@pytest.mark.skipif(not shutil.which("bwrap"), reason="bubblewrap unavailable")
def test_bwrap_denies_network_and_controller_home(tmp_path: Path):
    sandbox = ContainerSandbox()
    result = sandbox.run(
        tmp_path,
        "test ! -e /home/t/.config && "
        "! /usr/bin/python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 1)\"",
        env={"PATH": "/usr/bin:/bin"},
        network=False,
        limits=SandboxLimits(wall_seconds=5),
    )
    assert result.returncode == 0, result.stderr
