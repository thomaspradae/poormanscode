from pmc.executors.bash import _extract_json, _guard, UnsafeCommand
import pytest


def test_extract_json_with_noise():
    assert _extract_json('noise {"action":"done","summary":"x"} tail')["action"] == "done"


def test_guard_blocks_commit():
    with pytest.raises(UnsafeCommand):
        _guard("git commit -am nope")
