from datetime import UTC, datetime, timedelta

from pmc.cli import _attempt_duration
from pmc.providers import ProviderError


def test_running_attempt_duration_uses_wall_clock():
    attempt = {
        "duration_seconds": None,
        "status": "RUNNING",
        "started_at": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
    }
    assert _attempt_duration(attempt) >= 9


def test_provider_error_carries_structured_metadata():
    error = ProviderError(
        400,
        "provider HTTP 400 type=invalid_request_error code=bad_field",
        {},
        error_type="invalid_request_error",
        error_code="bad_field",
    )
    assert error.status_code == 400
    assert error.error_type == "invalid_request_error"
    assert error.error_code == "bad_field"
