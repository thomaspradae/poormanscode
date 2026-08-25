from pathlib import Path

from pmc.conformance import smoke_candidate
from pmc.db import Database
from pmc.domain import Candidate, ExecutionRequest, ExecutionResult, Job, Outcome
from pmc.executors import build_executor
from pmc.executors.jules import JulesExecutor
from pmc.executors.openhands import OpenHandsExecutor, _provider_failure


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
        "secrets": None,
    }
    assert calls["message"] == "do it"
    assert calls["ran"] is True
    assert calls["closed"] is True


def test_openhands_passes_nonnative_tool_setting_to_llm(tmp_path: Path):
    calls: dict[str, object] = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            calls["llm"] = kwargs

    class FakeConversation:
        def __init__(self, **_kwargs):
            pass

        def send_message(self, _message):
            pass

        def run(self):
            pass

        def close(self):
            pass

    executor = OpenHandsExecutor()
    executor._imports = lambda: (
        lambda value: value,
        FakeLLM,
        FakeConversation,
        object,
        lambda **kwargs: "agent",
    )
    candidate = Candidate.from_mapping(
        {
            "name": "gemini-openhands-nonnative",
            "executor": "openhands",
            "model": "gemini-3.7-flash",
            "openhands_model": "openai/gemini-3.7-flash",
            "native_tool_calling": False,
            "allow_local_unsandboxed": True,
        }
    )
    request = ExecutionRequest(
        Job("PMC-X", tmp_path, "task"), candidate, tmp_path, "do it", 1
    )

    result = executor.run(request)

    assert result.ok
    assert calls["llm"] == {
        "model": "openai/gemini-3.7-flash",
        "usage_id": "pmc:PMC-X",
        "native_tool_calling": False,
    }


def test_openhands_classifies_remote_provider_failures():
    assert _provider_failure(RuntimeError("litellm.RateLimitError: status 429"))[0:2] == (
        Outcome.RATE_LIMIT,
        429,
    )
    assert _provider_failure(RuntimeError("400 Function call is missing a thought_signature"))[0] == (
        Outcome.PROTOCOL_FAILURE
    )
    outcome, status, headers = _provider_failure(
        RuntimeError("ACPPromptError: [500] You have exhausted your daily quota on this model")
    )
    assert (outcome, status) == (Outcome.RATE_LIMIT, 429)
    assert headers["retry-after"] == "3600"
    assert _provider_failure(RuntimeError("ordinary agent failure"))[0] == Outcome.EXECUTOR_FAILURE


def test_openhands_acp_agent_receives_mapped_secret(monkeypatch, tmp_path: Path):
    calls: dict[str, object] = {}

    class FakeMetrics:
        accumulated_cost = 0
        accumulated_token_usage = None

    class FakeACPAgent:
        llm = type("LLM", (), {"metrics": FakeMetrics()})()

    class FakeConversation:
        def __init__(self, **kwargs):
            calls["conversation"] = kwargs

        def send_message(self, _message):
            pass

        def run(self):
            pass

        def close(self):
            pass

    monkeypatch.setenv("GEMINI_API_KEY_3", "credential-value")
    executor = OpenHandsExecutor()
    executor._imports = lambda: (
        lambda value: value,
        object,
        FakeConversation,
        object,
        lambda **kwargs: "unused",
    )
    executor._acp_agent = lambda _candidate: FakeACPAgent()
    candidate = Candidate.from_mapping(
        {
            "name": "gemini-cli-acp",
            "executor": "openhands",
            "provider": "gemini",
            "model": "gemini-3.7-flash",
            "api_key_env": "GEMINI_API_KEY_3",
            "agent_kind": "acp",
            "acp_server": "gemini-cli",
            "acp_command": ["gemini", "--acp"],
            "acp_api_key_env": "GEMINI_API_KEY",
            "allow_local_unsandboxed": True,
        }
    )
    request = ExecutionRequest(
        Job("PMC-X", tmp_path, "task"), candidate, tmp_path, "do it", 1
    )

    result = executor.run(request)

    assert result.ok
    assert calls["conversation"]["secrets"] == {
        "GEMINI_API_KEY": "credential-value"
    }


def test_acp_conformance_requires_real_file_write(monkeypatch, tmp_path: Path):
    def fake_run(_self, request):
        (request.worktree / "PMC_ACP_SMOKE.txt").write_text("PMC_ACP_TOOL_OK\n")
        return ExecutionResult(True, accounting_level="aggregate")

    monkeypatch.setattr(OpenHandsExecutor, "run", fake_run)
    candidate = Candidate.from_mapping(
        {
            "name": "gemini-cli-acp",
            "version": "1",
            "executor": "openhands",
            "model": "gemini-3.7-flash",
            "agent_kind": "acp",
            "acp_command": ["gemini", "--acp"],
        }
    )

    result = smoke_candidate(Database(tmp_path / "pmc.db"), candidate)

    assert result == {
        "candidate": "gemini-cli-acp",
        "generation": True,
        "tool": True,
        "status": "AVAILABLE",
    }


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
    assert any(
        "git add -A && git diff --binary --cached HEAD --" in command
        for command in calls["commands"]
    )
    assert any("git commit --allow-empty -qm baseline" in command for command in calls["commands"])


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
