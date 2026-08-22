from .base import Executor
from .bash import BashExecutor
from .openhands import OpenHandsExecutor
from .jules import JulesExecutor


def build_executor(name: str):
    if name == "bash":
        return BashExecutor()
    if name == "openhands":
        return OpenHandsExecutor()
    if name == "jules":
        return JulesExecutor()
    raise ValueError(f"unknown executor: {name}")


__all__ = ["Executor", "BashExecutor", "OpenHandsExecutor", "JulesExecutor", "build_executor"]
