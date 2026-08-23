from pmc.domain import ExecutionResult
from pmc.executors import build_executor


def test_all_registered_executors_share_result_contract():
    for name in ("bash", "openhands", "jules"):
        executor = build_executor(name)
        assert executor.name == name
    result = ExecutionResult(False)
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.cost_usd is None
    assert result.accounting_level == "unknown"
