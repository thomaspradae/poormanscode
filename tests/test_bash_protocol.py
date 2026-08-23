import json

import pytest

from pmc.domain import Candidate, ExecutionRequest, Job, Outcome
from pmc.executors.bash import BashExecutor, UnsafeCommand, _extract_json, _guard
from pmc.providers import ChatReply


def test_extract_json_with_noise():
    assert (
        _extract_json('noise {"action":"done","summary":"x"} tail')["action"] == "done"
    )


def test_guard_blocks_commit():
    with pytest.raises(UnsafeCommand):
        _guard("git commit -am nope")


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
