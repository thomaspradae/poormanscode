from pathlib import Path

from pmc.domain import Candidate, ExecutionRequest, ExecutionResult, Job
from pmc.executors import build_executor
from pmc.executors.openhands import OpenHandsExecutor
from pmc.executors.jules import JulesExecutor


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


def test_openhands_remote_workspace_uses_server_credential(monkeypatch, tmp_path: Path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    calls: dict[str, object] = {}

    class FakeSecret:
        def __init__(self, value):
            self.value = value

    class FakeLLM:
        def __init__(self, **kwargs):
            calls["llm"] = kwargs

    class FakeWorkspace:
        def __init__(self, **kwargs):
            calls["workspace"] = kwargs

        def file_upload(self, *_args):
            pass

        def execute_command(self, command):
            class Result:
                stdout = ""

            calls.setdefault("commands", []).append(command)
            return Result()

    class FakeConversation:
        def __init__(self, **kwargs):
            calls["conversation"] = kwargs

        def send_message(self, _message):
            pass

        def run(self):
            pass

        def close(self):
            pass

    monkeypatch.setenv("OPENHANDS_SESSION_KEY", "session-only")
    executor = OpenHandsExecutor()
    executor._imports = lambda: (
        FakeSecret,
        FakeLLM,
        FakeConversation,
        FakeWorkspace,
        lambda **kwargs: "agent",
    )
    candidate = Candidate.from_mapping(
        {
            "name": "openhands-remote",
            "executor": "openhands",
            "model": "provider/model",
            "api_key_env": "MODEL_KEY",
            "server_url": "http://ofi1.example:8010",
            "server_api_key_env": "OPENHANDS_SESSION_KEY",
        }
    )
    request = ExecutionRequest(
        Job("PMC-X", tmp_path, "task"), candidate, tmp_path, "do it", 1
    )

    result = executor.run(request)

    assert result.ok
    assert calls["workspace"] == {
        "host": "http://ofi1.example:8010",
        "api_key": "session-only",
    }


def test_jules_http_error_excludes_request_headers_and_key():
    import httpx

    request = httpx.Request(
        "POST",
        "https://jules.googleapis.com/v1alpha/sessions",
        headers={"x-goog-api-key": "must-never-appear"},
    )
    response = httpx.Response(
        404,
        request=request,
        json={"error": {"status": "NOT_FOUND", "message": "Source was not found"}},
    )
    error = JulesExecutor._http_error(
        httpx.HTTPStatusError("failure", request=request, response=response)
    )
    assert error == "Jules HTTP 404: NOT_FOUND Source was not found"
    assert "must-never-appear" not in error
