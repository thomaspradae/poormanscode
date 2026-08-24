import json
import subprocess

import pytest

from pmc.domain import Candidate, ExecutionRequest, Job, Outcome
from pmc.executors.bash import (
    BashExecutor,
    UnsafeCommand,
    _extract_json,
    _guard,
    _normalize_shell_command,
)
from pmc.providers import ChatReply
from pmc.providers.openai_compat import ProviderError


def test_extract_json_with_noise():
    assert (
        _extract_json('noise {"action":"done","summary":"x"} tail')["action"] == "done"
    )


def test_guard_blocks_commit():
    with pytest.raises(UnsafeCommand):
        _guard("git commit -am nope")


def test_groq_cmd_array_is_normalized_to_ordered_shell_script():
    assert _normalize_shell_command(["pwd", "npm test"]) == "pwd\nnpm test"
    assert _normalize_shell_command([]) is None
    assert _normalize_shell_command(["pwd", 42]) is None


@pytest.mark.parametrize(
    ("reply", "outcome"),
    [
        ({"action": "bash", "command": "sudo true"}, Outcome.SECURITY_FAILURE),
        ({"action": "wat"}, Outcome.FORMAT_FAILURE),
    ],
)
def test_bash_failure_classification(tmp_path, monkeypatch, reply, outcome):
    candidate = Candidate(
        name="test",
        executor="bash",
        model="m",
        base_url="http://unused",
        max_turns=1,
        sandbox="guarded",
        network=True,
    )
    request = ExecutionRequest(
        Job("PMC-1", tmp_path, "task"), candidate, tmp_path, "task", 1
    )
    monkeypatch.setattr(
        "pmc.executors.bash.OpenAICompatibleClient.chat",
        lambda *args, **kwargs: ChatReply(json.dumps(reply)),
    )
    result = BashExecutor().run(request)
    assert result.outcome == outcome


def test_bash_executes_native_shell_tool(tmp_path, monkeypatch):
    candidate = Candidate(
        name="test",
        executor="bash",
        model="m",
        base_url="http://unused",
        max_turns=2,
        sandbox="guarded",
        network=True,
    )
    request = ExecutionRequest(
        Job("PMC-1", tmp_path, "task"), candidate, tmp_path, "task", 1
    )
    replies = iter(
        [
            ChatReply(
                "",
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "arguments": json.dumps({"command": "pwd"}),
                        },
                    }
                ],
            ),
            ChatReply('{"action":"done","summary":"finished"}'),
        ]
    )
    monkeypatch.setattr(
        "pmc.executors.bash.OpenAICompatibleClient.chat",
        lambda *args, **kwargs: next(replies),
    )
    monkeypatch.setattr(
        BashExecutor,
        "_command",
        lambda self, request, command: subprocess.CompletedProcess(
            ["bash"], 0, "/repo\n", ""
        ),
    )

    result = BashExecutor().run(request)

    assert result.ok
    assert result.summary == "finished"


def test_bash_executes_textual_shell_tool_for_local_models(tmp_path, monkeypatch):
    candidate = Candidate(
        name="local",
        executor="bash",
        model="m",
        base_url="http://unused",
        max_turns=2,
        sandbox="guarded",
        network=True,
    )
    request = ExecutionRequest(
        Job("PMC-1", tmp_path, "task"), candidate, tmp_path, "task", 1
    )
    replies = iter(
        [
            ChatReply(
                json.dumps(
                    {"name": "shell", "arguments": {"command": "pwd"}}
                )
            ),
            ChatReply('{"action":"done","summary":"finished"}'),
        ]
    )
    monkeypatch.setattr(
        "pmc.executors.bash.OpenAICompatibleClient.chat",
        lambda *args, **kwargs: next(replies),
    )
    commands = []
    monkeypatch.setattr(
        BashExecutor,
        "_command",
        lambda self, request, command: (
            commands.append(command)
            or subprocess.CompletedProcess(["bash"], 0, "/repo\n", "")
        ),
    )

    result = BashExecutor().run(request)

    assert result.ok
    assert commands == ["pwd"]


def test_bash_accepts_native_json_completion_tool(tmp_path, monkeypatch):
    candidate = Candidate(
        name="test",
        executor="bash",
        model="m",
        base_url="http://unused",
        max_turns=1,
        sandbox="guarded",
        network=True,
    )
    request = ExecutionRequest(
        Job("PMC-1", tmp_path, "task"), candidate, tmp_path, "task", 1
    )
    monkeypatch.setattr(
        "pmc.executors.bash.OpenAICompatibleClient.chat",
        lambda *args, **kwargs: ChatReply(
            "",
            tool_calls=[
                {
                    "id": "done-1",
                    "type": "function",
                    "function": {
                        "name": "JSON",
                        "arguments": json.dumps(
                            {"action": "done", "summary": "finished"}
                        ),
                    },
                }
            ],
        ),
    )

    result = BashExecutor().run(request)

    assert result.ok
    assert result.summary == "finished"


def test_bash_retries_provider_server_error(tmp_path, monkeypatch):
    candidate = Candidate(
        name="test",
        executor="bash",
        model="m",
        base_url="http://unused",
        max_turns=1,
        sandbox="guarded",
        network=True,
        extra={"server_error_retries": 1},
    )
    request = ExecutionRequest(
        Job("PMC-1", tmp_path, "task"), candidate, tmp_path, "task", 1
    )
    replies = iter(
        [
            ProviderError(500, "temporary", {}),
            ChatReply('{"action":"done","summary":"finished"}'),
        ]
    )

    def chat(*args, **kwargs):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    sleeps = []
    monkeypatch.setattr("pmc.executors.bash.OpenAICompatibleClient.chat", chat)
    monkeypatch.setattr("pmc.executors.bash.time.sleep", sleeps.append)

    result = BashExecutor().run(request)

    assert result.ok
    assert sleeps == [1.0]
    assert result.raw_metrics["turns"][0]["provider_server_error"] == 500


def test_bash_honors_retry_after_within_attempt(tmp_path, monkeypatch):
    candidate = Candidate(
        name="test",
        executor="bash",
        model="m",
        base_url="http://unused",
        max_turns=1,
        sandbox="guarded",
        network=True,
        extra={"rate_limit_retries": 1},
    )
    request = ExecutionRequest(
        Job("PMC-1", tmp_path, "task"), candidate, tmp_path, "task", 1
    )
    replies = iter(
        [
            ProviderError(429, "limited", {"retry-after": "2"}),
            ChatReply('{"action":"done","summary":"finished"}'),
        ]
    )

    def chat(*args, **kwargs):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    sleeps = []
    monkeypatch.setattr("pmc.executors.bash.OpenAICompatibleClient.chat", chat)
    monkeypatch.setattr("pmc.executors.bash.time.sleep", sleeps.append)

    result = BashExecutor().run(request)

    assert result.ok
    assert sleeps == [2.0]


def test_bash_retries_malformed_provider_tool_call_within_attempt(
    tmp_path, monkeypatch
):
    candidate = Candidate(
        name="test",
        executor="bash",
        model="m",
        base_url="http://unused",
        max_turns=1,
        sandbox="guarded",
        network=True,
        extra={"tool_protocol_retries": 1},
    )
    request = ExecutionRequest(
        Job("PMC-1", tmp_path, "task"), candidate, tmp_path, "task", 1
    )
    replies = iter(
        [
            ProviderError(
                400,
                "bad tool JSON",
                {},
                error_code="tool_use_failed",
                failed_generation={"reason": "invalid JSON"},
            ),
            ChatReply('{"action":"done","summary":"finished"}'),
        ]
    )

    def chat(*args, **kwargs):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("pmc.executors.bash.OpenAICompatibleClient.chat", chat)

    result = BashExecutor().run(request)

    assert result.ok
    assert result.raw_metrics["turns"][0]["tool_protocol_retry"] == 1
