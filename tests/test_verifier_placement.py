from pmc.db import Database
from pmc.domain import Candidate, ExecutionResult, Job
from pmc.verifier import select_verifier_runtime


def test_toolchain_placement_overrides_executor_placement():
    sandbox, config, source = select_verifier_runtime(
        {"toolchain": "node"},
        {
            "node": {
                "verifier_sandbox": "remote-lxd",
                "remote_host": "ofi1",
                "remote_instance": "node-worker",
            }
        },
        {"verifier_sandbox": "bwrap"},
        "guarded",
    )
    assert sandbox == "remote-lxd"
    assert config["remote_host"] == "ofi1"
    assert source == "toolchain:node"


def test_repository_cannot_supply_remote_machine_configuration():
    sandbox, config, source = select_verifier_runtime(
        {
            "toolchain": "node",
            "remote_host": "attacker.example",
            "verifier_sandbox": "guarded",
        },
        {},
        {},
        "bwrap",
    )
    assert (sandbox, config, source) == ("bwrap", {}, "controller-default")


def test_candidate_placement_remains_a_compatibility_fallback():
    sandbox, config, source = select_verifier_runtime(
        {"toolchain": "node"},
        {},
        {"verifier_sandbox": "remote-lxd", "remote_host": "ofi1"},
        "bwrap",
    )
    assert sandbox == "remote-lxd"
    assert config["remote_host"] == "ofi1"
    assert source == "candidate"


def test_successful_reverification_promotes_existing_attempt_without_duplication(
    tmp_path,
):
    db = Database(tmp_path / "pmc.db")
    job = Job("PMC-X", tmp_path, "task")
    db.create_job(job)
    attempt = db.begin_attempt(job.id, 1, Candidate("jules", "jules"), "forced", 0)
    db.finish_attempt(
        attempt, "VERIFY_FAILED", ExecutionResult(True), 1.0, outcome="TEST_FAILURE"
    )

    db.mark_attempt_ready_after_reverification(attempt)

    detail = db.job_detail(job.id)
    assert len(detail["attempts"]) == 1
    assert detail["attempts"][0]["status"] == "READY"
    assert detail["attempts"][0]["outcome"] == "SUCCESS"
