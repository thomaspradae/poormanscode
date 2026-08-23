from __future__ import annotations

from typing import Protocol

from ..domain import ExecutionRequest, ExecutionResult


class Executor(Protocol):
    name: str

    def run(self, request: ExecutionRequest) -> ExecutionResult: ...
