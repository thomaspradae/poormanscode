from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

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


def test_context_capacity_failure_is_not_model_quality_failure():
    error = RuntimeError(
        "HTTP 503: request context and output allowance exceed the selected "
        "lane's sustainable request limit"
    )
    outcome, status, _headers = _provider_failure(error)
    assert outcome == Outcome.RESOURCE_FAILURE
    assert status == 503


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


def _assert_remote_workspace_calls(calls):
    assert calls["workspace"] == {
        "host": "http://ofi1.example:8010",
        "working_dir": "/tmp/pmc-pmc-x-1",
        "api_key": "session-only",
    }
    assert any(
        "git add -A && git diff --binary --cached HEAD --" in command
        for command in calls["commands"]
    )
    assert any(
        "git commit --allow-empty -qm baseline" in command
        for command in calls["commands"]
    )


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


def test_openhands_pooled_llm_reserves_each_request_and_rotates_credentials(
    monkeypatch,
):
    calls: list[str] = []

    class FakeLLM(BaseModel):
        api_key: str | None = None
        num_retries: int = 5
        stream: bool = True
        max_output_tokens: int | None = 128

        def completion(self, _messages, tools=None, **_kwargs):
            del tools
            calls.append(str(self.api_key))
            if self.api_key == "lane-one":
                error = RuntimeError("HTTP 429")
                error.status_code = 429  # type: ignore[attr-defined]
                raise error
            return SimpleNamespace(
                raw_response={
                    "id": "request-two",
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                }
            )

    class Accounting:
        def __init__(self):
            self.reserved: list[int] = []
            self.failed: list[str] = []
            self.succeeded: list[object] = []

        def reserve(self, turn, _messages, _max_output):
            self.reserved.append(turn)
            lane = "LANE_ONE" if turn == 1 else "LANE_TWO"
            return SimpleNamespace(api_key_env=lane)

        def fail(self, ticket, error):
            self.failed.append(ticket.api_key_env + ":" + str(error))

        def succeed(self, _ticket, reply):
            self.succeeded.append(reply)

    monkeypatch.setenv("LANE_ONE", "lane-one")
    monkeypatch.setenv("LANE_TWO", "lane-two")
    accounting = Accounting()
    llm = OpenHandsExecutor()._pooled_llm(
        FakeLLM, lambda value: value, {}, accounting, max_failovers=1
    )

    response = llm.completion([{"role": "user", "content": "edit it"}])

    assert response.raw_response["id"] == "request-two"
    assert accounting.reserved == [1, 2]
    assert accounting.failed == ["LANE_ONE:HTTP 429"]
    assert accounting.succeeded[0].input_tokens == 11
    assert accounting.succeeded[0].output_tokens == 7
    assert calls == ["lane-one", "lane-two"]
    assert llm.num_retries == 0
    assert llm.stream is False


def test_openhands_classifies_remote_provider_failures():
    assert _provider_failure(RuntimeError("litellm.RateLimitError: status 429"))[
        0:2
    ] == (
        Outcome.RATE_LIMIT,
        429,
    )
    assert _provider_failure(
        RuntimeError("400 Function call is missing a thought_signature")
    )[0] == (Outcome.PROTOCOL_FAILURE)
    outcome, status, headers = _provider_failure(
        RuntimeError(
            "ACPPromptError: [500] You have exhausted your daily quota on this model"
        )
    )
    assert (outcome, status) == (Outcome.RATE_LIMIT, 429)
    assert headers["retry-after"] == "3600"
    assert (
        _provider_failure(RuntimeError("ordinary agent failure"))[0]
        == Outcome.EXECUTOR_FAILURE
    )


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
    assert calls["conversation"]["secrets"] == {"GEMINI_API_KEY": "credential-value"}


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
    _assert_remote_workspace_calls(calls)


def test_openhands_preserves_remote_partial_diff_when_agent_hits_limit(
    monkeypatch, tmp_path: Path
):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    target = tmp_path / "value.txt"
    target.write_text("before\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "value.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "baseline"], check=True
    )
    target.write_text("after\n")
    patch = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--binary", "HEAD", "--"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    target.write_text("before\n")

    class FakeSecret:
        def __init__(self, value):
            self.value = value

    class FakeLLM:
        def __init__(self, **_kwargs):
            pass

    class FakeWorkspace:
        def __init__(self, **_kwargs):
            pass

        def file_upload(self, *_args):
            pass

        def execute_command(self, command):
            class Result:
                stdout = patch if "git diff --binary" in command else ""

            return Result()

    class State:
        events = []
        execution_status = "FAILED"

    class FakeConversation:
        state = State()
        id = "partial"

        def __init__(self, **_kwargs):
            pass

        def send_message(self, _message):
            pass

        def run(self):
            raise RuntimeError("MaxIterationsReached")

        def close(self):
            pass

    monkeypatch.setenv("OPENHANDS_SESSION_KEY", "session-only")
    executor = OpenHandsExecutor()
    executor._imports = lambda: (
        FakeSecret,
        FakeLLM,
        FakeConversation,
        FakeWorkspace,
        lambda **_kwargs: "agent",
    )
    candidate = Candidate.from_mapping(
        {
            "name": "openhands-remote",
            "executor": "openhands",
            "model": "provider/model",
            "server_url": "http://ofi1.example:8010",
            "server_api_key_env": "OPENHANDS_SESSION_KEY",
        }
    )
    result = executor.run(
        ExecutionRequest(
            Job("PMC-X", tmp_path, "task"), candidate, tmp_path, "do it", 1
        )
    )
    assert result.ok is False
    assert target.read_text() == "after\n"
    assert result.raw_metrics["agent"]["meaningful_progress"] is True


def test_openhands_gateway_bind_failure_is_resource_failure(
    monkeypatch, tmp_path: Path
):
    from pmc.domain import Outcome
    from pmc.provider_gateway import ProviderGateway

    def fail_start(self):
        raise OSError(99, "Cannot assign requested address")

    monkeypatch.setattr(ProviderGateway, "start", fail_start)
    candidate = Candidate.from_mapping(
        {
            "name": "openhands-remote",
            "executor": "openhands",
            "provider": "provider",
            "model": "provider/model",
            "base_url": "https://provider.invalid/v1",
            "server_url": "http://ofi1.invalid:8010",
            "controller_gateway_host": "100.64.0.1",
        }
    )
    request = ExecutionRequest(
        Job("PMC-X", tmp_path, "task"),
        candidate,
        tmp_path,
        "do it",
        1,
        accounting=object(),
    )
    result = OpenHandsExecutor().run(request)
    assert result.ok is False
    assert result.outcome == Outcome.RESOURCE_FAILURE


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
