from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .domain import Outcome


class RetryAction(StrEnum):
    COMPLETE = "COMPLETE"
    RETRY_SAME_WITH_EVIDENCE = "RETRY_SAME_WITH_EVIDENCE"
    RETRY_ALTERNATE = "RETRY_ALTERNATE"
    REQUEUE = "REQUEUE"
    BLOCK = "BLOCK"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True)
class RetryPolicy:
    action: RetryAction
    affects_quality: bool


POLICIES: dict[Outcome, RetryPolicy] = {
    Outcome.SUCCESS: RetryPolicy(RetryAction.COMPLETE, True),
    Outcome.PROVIDER_FAILURE: RetryPolicy(RetryAction.RETRY_ALTERNATE, False),
    Outcome.RATE_LIMIT: RetryPolicy(RetryAction.RETRY_ALTERNATE, False),
    Outcome.RESOURCE_FAILURE: RetryPolicy(RetryAction.REQUEUE, False),
    Outcome.EXECUTOR_FAILURE: RetryPolicy(RetryAction.RETRY_ALTERNATE, False),
    Outcome.EXECUTOR_CRASH: RetryPolicy(RetryAction.RETRY_ALTERNATE, False),
    Outcome.TIMEOUT: RetryPolicy(RetryAction.RETRY_ALTERNATE, False),
    Outcome.PROTOCOL_FAILURE: RetryPolicy(RetryAction.RETRY_SAME_WITH_EVIDENCE, True),
    Outcome.FORMAT_FAILURE: RetryPolicy(RetryAction.RETRY_SAME_WITH_EVIDENCE, True),
    Outcome.TEST_FAILURE: RetryPolicy(RetryAction.RETRY_SAME_WITH_EVIDENCE, True),
    Outcome.LINT_FAILURE: RetryPolicy(RetryAction.RETRY_SAME_WITH_EVIDENCE, True),
    Outcome.BUILD_FAILURE: RetryPolicy(RetryAction.RETRY_SAME_WITH_EVIDENCE, True),
    Outcome.TYPECHECK_FAILURE: RetryPolicy(RetryAction.RETRY_SAME_WITH_EVIDENCE, True),
    Outcome.SCOPE_FAILURE: RetryPolicy(RetryAction.RETRY_ALTERNATE, True),
    Outcome.SECURITY_FAILURE: RetryPolicy(RetryAction.BLOCK, True),
    Outcome.POLICY_FAILURE: RetryPolicy(RetryAction.BLOCK, True),
    Outcome.REVIEW_FAILURE: RetryPolicy(RetryAction.RETRY_SAME_WITH_EVIDENCE, True),
    Outcome.HUMAN_REJECT: RetryPolicy(RetryAction.RETRY_SAME_WITH_EVIDENCE, True),
    Outcome.CANCELLED: RetryPolicy(RetryAction.TERMINAL, False),
}


def policy_for(outcome: Outcome | str) -> RetryPolicy:
    return POLICIES[Outcome(outcome)]
