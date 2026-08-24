from pathlib import Path

import pytest

from pmc.capabilities import CapabilityRegistry, infer_required_capabilities
from pmc.db import Database
from pmc.domain import Candidate, Job
from pmc.scheduler import NoAvailableCandidate, Scheduler


def test_skeletal_unity_requires_editor_and_native_scaffolder():
    assert infer_required_capabilities("Create a Unity game", skeletal=True) == [
        "scaffolder:unity",
        "unity-editor",
    ]


def test_scheduler_blocks_before_attempt_when_capability_missing(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    registry = CapabilityRegistry(db)
    scheduler = Scheduler(db, 0.2, 1, registry)
    job = Job(
        "PMC-000001",
        tmp_path,
        "Unity",
        constraints={"required_capabilities": ["definitely-absent-capability"]},
    )
    candidate = Candidate("worker", "bash")
    with pytest.raises(NoAvailableCandidate) as caught:
        scheduler.choose(job, [candidate])
    assert "missing capabilities" in caught.value.unavailable["worker"]


def test_declared_remote_capability_is_eligible(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    registry = CapabilityRegistry(db)
    scheduler = Scheduler(db, 0.2, 1, registry)
    job = Job(
        "PMC-000001",
        tmp_path,
        "task",
        constraints={"required_capabilities": ["unity-editor"]},
    )
    candidate = Candidate.from_mapping(
        {"name": "remote", "executor": "bash", "capabilities": ["unity-editor"]}
    )
    assert scheduler.choose(job, [candidate]).candidate.name == "remote"


def test_trustworthy_next_site_does_not_require_rust():
    required = infer_required_capabilities(
        "Build a trustworthy local Next.js website", skeletal=True
    )
    assert "rustc" not in required
    assert "cargo" not in required
    assert {"node", "npm", "scaffolder:create-next-app"} <= set(required)
