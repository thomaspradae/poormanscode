from pmc.domain import Outcome
from pmc.retry import POLICIES, RetryAction, policy_for


def test_every_outcome_has_explicit_policy():
    assert set(POLICIES) == set(Outcome)
    assert policy_for(Outcome.RATE_LIMIT).affects_quality is False
    assert policy_for(Outcome.SECURITY_FAILURE).action == RetryAction.BLOCK
    assert (
        policy_for(Outcome.TEST_FAILURE).action == RetryAction.RETRY_SAME_WITH_EVIDENCE
    )
