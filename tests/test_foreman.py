from pathlib import Path

import pytest

from pmc.db import Database
from pmc.domain import Candidate, Job, JobState
from pmc.foreman import validate_plan
from pmc.scheduler import Scheduler


def _plan():
    return {
        "tasks": [
            {
                "id": "bootstrap",
                "request": "Create the project skeleton",
                "acceptance": ["Project structure exists"],
                "depends_on": [],
            },
            {
                "id": "pitch",
                "request": "Create the pitch",
                "acceptance": ["Pitch exists"],
                "depends_on": ["bootstrap"],
            },
            {
                "id": "camera",
                "request": "Create the camera",
                "acceptance": ["Camera exists"],
                "depends_on": ["bootstrap"],
            },
            {
                "id": "integration",
                "request": "Integrate the scene",
                "acceptance": ["Scene is playable"],
                "depends_on": ["pitch", "camera"],
            },
        ]
    }


def test_plan_rejects_cycles():
    plan = {
        "tasks": [
            {"id": "a", "request": "A", "depends_on": ["b"]},
            {"id": "b", "request": "B", "depends_on": ["a"]},
        ]
    }
    with pytest.raises(ValueError, match="cycle"):
        validate_plan(plan)


def test_plan_requires_one_terminal_integration_task():
    plan = {
        "tasks": [
            {"id": "pitch", "request": "Pitch", "depends_on": []},
            {"id": "camera", "request": "Camera", "depends_on": []},
        ]
    }
    with pytest.raises(ValueError, match="one terminal integration task"):
        validate_plan(plan)


def test_dependency_queue_releases_only_accepted_prerequisites(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    feature_id = db.next_feature_id()
    db.create_feature(feature_id, tmp_path, "Football", "Build football", "main")
    plan = validate_plan(_plan())
    db.save_feature_plan(feature_id, plan, "foreman")
    jobs = []
    for position, task in enumerate(plan["tasks"], 1):
        job = Job(
            f"PMC-{position:06d}",
            tmp_path,
            task["request"],
            acceptance=task.get("acceptance", []),
        )
        jobs.append((job, task["id"], position - 1, task.get("depends_on", [])))
    db.approve_feature_plan(feature_id, jobs)

    assert db.queued_jobs() == ["PMC-000001"]
    db.set_state("PMC-000001", JobState.READY)
    assert db.queued_jobs() == []
    db.set_state("PMC-000001", JobState.ACCEPTED)
    assert db.queued_jobs() == ["PMC-000002", "PMC-000003"]
    db.set_state("PMC-000002", JobState.ACCEPTED)
    assert db.queued_jobs() == ["PMC-000003"]
    db.set_state("PMC-000003", JobState.ACCEPTED)
    assert db.queued_jobs() == ["PMC-000004"]


def test_standalone_jobs_remain_compatible(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    db.create_job(Job("PMC-000001", tmp_path, "ordinary job"))
    assert db.queued_jobs() == ["PMC-000001"]


def test_dependency_commits_are_topological_and_unique(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    feature_id = db.next_feature_id()
    db.create_feature(feature_id, tmp_path, "Football", "Build football", "main")
    plan = validate_plan(_plan())
    db.save_feature_plan(feature_id, plan, "foreman")
    jobs = []
    for position, task in enumerate(plan["tasks"], 1):
        job = Job(f"PMC-{position:06d}", tmp_path, task["request"])
        jobs.append((job, task["id"], position - 1, task.get("depends_on", [])))
    db.approve_feature_plan(feature_id, jobs)
    with db.connect() as conn:
        for job_id, commit in (
            ("PMC-000001", "commit-bootstrap"),
            ("PMC-000002", "commit-pitch"),
            ("PMC-000003", "commit-camera"),
        ):
            conn.execute(
                "UPDATE jobs SET state='ACCEPTED',accepted_commit=? WHERE id=?",
                (commit, job_id),
            )

    assert db.dependency_commits("PMC-000004") == [
        "commit-bootstrap",
        "commit-pitch",
        "commit-camera",
    ]


def test_candidate_order_precedes_empirical_routing(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    job = Job(
        "PMC-000001",
        tmp_path,
        "task",
        constraints={"_candidate_order": ["groq", "jules", "qwen"]},
    )
    candidates = [
        Candidate(name="qwen", executor="bash"),
        Candidate(name="jules", executor="jules"),
        Candidate(name="groq", executor="bash"),
    ]
    scheduler = Scheduler(db, exploration_rate=1.0, min_samples=5)

    first = scheduler.choose(job, candidates)
    second = scheduler.choose(job, candidates, exclude={"groq"}, attempt_no=2)
    third = scheduler.choose(job, candidates, exclude={"groq", "jules"}, attempt_no=3)

    assert [first.candidate.name, second.candidate.name, third.candidate.name] == [
        "groq",
        "jules",
        "qwen",
    ]
