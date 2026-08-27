from pathlib import Path

import pytest

from pmc.db import Database
from pmc.domain import Candidate, Job, JobState
from pmc.foreman import validate_plan, validate_program_plan
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


def test_program_plan_requires_explicit_acceptance_and_is_bounded():
    missing_acceptance = _plan()
    missing_acceptance["tasks"][1]["acceptance"] = []
    with pytest.raises(ValueError, match="requires explicit verification"):
        validate_program_plan(missing_acceptance)
    plan = _plan()
    for task in plan["tasks"]:
        task["acceptance"] = [f"{task['id']} verified"]
    assert validate_program_plan(plan)["kind"] == "program"

    too_large = {
        "tasks": [
            {
                "id": f"n{index}",
                "request": f"node {index}",
                "acceptance": ["verified"],
                "depends_on": [] if index == 0 else [f"n{index - 1}"],
            }
            for index in range(51)
        ]
    }
    with pytest.raises(ValueError, match="50-task"):
        validate_program_plan(too_large)


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


def test_program_dependencies_release_on_verified_internal_commit(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    feature_id = db.next_feature_id()
    db.create_feature(
        feature_id,
        tmp_path,
        "Football V0",
        "Build the lab",
        "main",
        mode="AUTONOMOUS_PROGRAM",
        max_workers=3,
    )
    plan = validate_plan(_plan())
    db.save_feature_plan(feature_id, plan, "foreman")
    jobs = []
    for position, task in enumerate(plan["tasks"], 1):
        job = Job(f"PMC-{position:06d}", tmp_path, task["request"])
        jobs.append((job, task["id"], position - 1, task.get("depends_on", [])))
    db.approve_feature_plan(feature_id, jobs)

    db.set_state("PMC-000001", JobState.READY)
    assert db.program_ready_jobs(feature_id, 10) == []
    assert db.promote_program_task("PMC-000001", "verified-bootstrap")
    assert db.get_job("PMC-000001").state == JobState.READY
    assert db.program_ready_jobs(feature_id, 10) == ["PMC-000002", "PMC-000003"]

    with pytest.raises(RuntimeError, match="terminal"):
        db.set_state("PMC-000004", JobState.READY)
        db.promote_program_task("PMC-000004", "must-not-promote")


def test_program_dependency_commits_use_promotions_without_fake_acceptance(
    tmp_path: Path,
):
    db = Database(tmp_path / "pmc.db")
    feature_id = db.next_feature_id()
    db.create_feature(
        feature_id,
        tmp_path,
        "Program",
        "Build",
        "main",
        mode="AUTONOMOUS_PROGRAM",
    )
    plan = validate_plan(_plan())
    db.save_feature_plan(feature_id, plan, None)
    jobs = []
    for position, task in enumerate(plan["tasks"], 1):
        job = Job(f"PMC-{position:06d}", tmp_path, task["request"])
        jobs.append((job, task["id"], position, task.get("depends_on", [])))
    db.approve_feature_plan(feature_id, jobs)
    with db.connect() as conn:
        for job_id, commit in (
            ("PMC-000001", "verified-bootstrap"),
            ("PMC-000002", "verified-pitch"),
            ("PMC-000003", "verified-camera"),
        ):
            conn.execute(
                """UPDATE jobs SET state='READY_FOR_REVIEW' WHERE id=?""", (job_id,)
            )
            conn.execute(
                """UPDATE feature_tasks SET promotion_state='VERIFIED_FOR_CHAINING',
                verified_commit=? WHERE job_id=?""",
                (commit, job_id),
            )

    assert db.dependency_commits("PMC-000004") == [
        "verified-bootstrap",
        "verified-pitch",
        "verified-camera",
    ]
    assert all(db.get_job(f"PMC-{n:06d}").state != JobState.ACCEPTED for n in range(1, 4))


def test_approved_program_plan_is_immutable(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    feature_id = db.next_feature_id()
    db.create_feature(
        feature_id,
        tmp_path,
        "Program",
        "Build",
        "main",
        mode="AUTONOMOUS_PROGRAM",
    )
    plan = validate_plan(_plan())
    db.save_feature_plan(feature_id, plan, None)
    jobs = [
        (
            Job(f"PMC-{position + 1:06d}", tmp_path, task["request"]),
            task["id"],
            position,
            task.get("depends_on", []),
        )
        for position, task in enumerate(plan["tasks"])
    ]
    db.approve_feature_plan(feature_id, jobs)
    with pytest.raises(RuntimeError, match="immutable"):
        db.save_feature_plan(feature_id, plan, None)


def test_only_terminal_human_acceptance_accepts_program(tmp_path: Path):
    db = Database(tmp_path / "pmc.db")
    feature_id = db.next_feature_id()
    db.create_feature(
        feature_id,
        tmp_path,
        "Program",
        "Build",
        "main",
        mode="AUTONOMOUS_PROGRAM",
    )
    plan = validate_plan(_plan())
    db.save_feature_plan(feature_id, plan, None)
    jobs = [
        (
            Job(f"PMC-{position + 1:06d}", tmp_path, task["request"]),
            task["id"],
            position,
            task.get("depends_on", []),
        )
        for position, task in enumerate(plan["tasks"])
    ]
    db.approve_feature_plan(feature_id, jobs)
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET state='READY_FOR_REVIEW' WHERE id='PMC-000004'")
        attempt_id = conn.execute(
            """INSERT INTO attempts(job_id,attempt_no,candidate,executor,role,
            selection_mode,status,started_at) VALUES(
            'PMC-000004',1,'candidate','jules','builder','forced','READY','now')"""
        ).lastrowid
    db.complete_acceptance("PMC-000004", int(attempt_id), "final-commit")
    assert db.get_feature(feature_id)["state"] == "ACCEPTED"
    assert db.get_feature(feature_id)["integration_commit"] == "final-commit"
    assert db.get_job("PMC-000001").state == JobState.QUEUED


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
