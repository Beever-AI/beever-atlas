"""Tests for Fix #10: ``_capability_error_to_dict`` translates
``ServiceUnavailable`` into the structured ``service_unavailable`` error
shape returned by ADK tools.
"""

from __future__ import annotations

from beever_atlas.agents.tools.orchestration_tools import _capability_error_to_dict
from beever_atlas.capabilities.errors import (
    ChannelAccessDenied,
    CooldownActive,
    JobNotFound,
    ServiceUnavailable,
)


def test_service_unavailable_translates_to_structured_dict():
    result = _capability_error_to_dict(ServiceUnavailable("stores"))
    assert result == {"error": "service_unavailable", "service": "stores"}


def test_known_errors_still_translate():
    """Smoke check that the added branch did not break existing branches."""
    assert _capability_error_to_dict(ChannelAccessDenied("ch-a")) == {
        "error": "channel_access_denied",
        "channel_id": "ch-a",
    }
    assert _capability_error_to_dict(CooldownActive(60)) == {
        "error": "cooldown_active",
        "retry_after_seconds": 60,
    }
    assert _capability_error_to_dict(JobNotFound("job-x")) == {
        "error": "job_not_found",
        "job_id": "job-x",
    }
