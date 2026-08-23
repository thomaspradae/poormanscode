import os
import sys
import time
from pathlib import Path

from pmc.sandbox import GuardedSandbox, SandboxLimits
from pmc.reporting import Reporter
import pytest


def test_process_tree_is_killed_on_timeout(tmp_path: Path):
    script = tmp_path / "tree.py"
    script.write_text(
        "import os, subprocess, sys, time\n"
        "level = int(sys.argv[1])\n"
        "open(('parent','child','grandchild')[level] + '.pid', 'w').write(str(os.getpid()))\n"
        "if level < 2: subprocess.Popen([sys.executable, __file__, str(level + 1)])\n"
        "time.sleep(60)\n"
    )
    result = GuardedSandbox().run(
        tmp_path,
        f"{sys.executable} tree.py 0",
        env={"PATH": "/usr/bin:/bin"},
        network=True,
        limits=SandboxLimits(wall_seconds=1),
    )
    assert result.returncode == 124
    time.sleep(0.1)
    for name in ("parent.pid", "child.pid", "grandchild.pid"):
        pid = int((tmp_path / name).read_text())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise AssertionError(f"process survived timeout: {name}={pid}")


def test_aggregate_workspace_limits(tmp_path: Path):
    result = GuardedSandbox().run(
        tmp_path,
        "for n in 1 2 3 4 5; do printf 1234567890 > file-$n; done",
        env={"PATH": "/usr/bin:/bin"},
        network=True,
        limits=SandboxLimits(workspace_bytes=40, workspace_files=10),
    )
    assert result.returncode == 75
    assert "WORKSPACE_LIMIT" in result.stderr


def test_aggregate_artifact_limit(tmp_path: Path):
    reporter = Reporter(tmp_path, max_job_bytes=10)
    reporter.text("job", "a", "12345")
    with pytest.raises(RuntimeError, match="ARTIFACT_LIMIT"):
        reporter.text("job", "b", "678901")
