from pathlib import Path

from pmc.domain import Candidate, ExecutionRequest, ExecutionResult, Job
from pmc.executors import build_executor
from pmc.executors.openhands import OpenHandsExecutor


def test_all_registered_executors_share_result_contract():
    for name in ("bash", "openhands", "jules"):
        executor = build_executor(name)
        assert executor.name == name
    result = ExecutionResult(False)
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.cost_usd is None
    assert result.accounting_level == "unknown"


def test_openhands_uses_bounded_stuck_detecting_conversation(tmp_path: Path):
    calls: dict[str, object] = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            calls["llm"] = kwargs

    class FakeConversation:
        def __init__(self, **kwargs):
            calls["conversation"] = kwargs

        def send_message(self, message):
            calls["message"] = message

        def run(self):
            calls["ran"] = True

        def close(self):
            calls["closed"] = True

    executor = OpenHandsExecutor()
    executor._imports = lambda: (  # type: ignore[method-assign]
        lambda value: value,
        FakeLLM,
        FakeConversation,
        object,
        lambda **kwargs: "agent",
    )
    candidate = Candidate.from_mapping(
        {
            "name": "openhands",
            "executor": "openhands",
            "model": "provider/model",
            "allow_local_unsandboxed": True,
            "max_turns": 37,
        }
    )
    request = ExecutionRequest(
        Job("PMC-X", tmp_path, "task"), candidate, tmp_path, "do it", 1
    )

    result = executor.run(request)

    assert result.ok
    assert result.accounting_level == "aggregate"
    assert calls["conversation"] == {
        "agent": "agent",
        "workspace": str(tmp_path),
        "max_iteration_per_run": 37,
        "stuck_detection": True,
        "visualizer": None,
    }
    assert calls["message"] == "do it"
    assert calls["ran"] is True
    assert calls["closed"] is True
