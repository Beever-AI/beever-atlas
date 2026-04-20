"""Tests for TruncationReport returned by recover_truncated_json_with_report."""

from __future__ import annotations

from beever_atlas.services.json_recovery import (
    TruncationReport,
    recover_truncated_json_with_report,
)


def test_truncation_report_recovered_count_and_estimated_lost() -> None:
    """Feed a deliberately truncated JSON and verify report fields."""
    # Truncated mid-object — 3 complete items, 1 cut off
    truncated = '{"facts":[{"id":1},{"id":2},{"id":3},{"id":'

    result, report = recover_truncated_json_with_report(truncated)

    assert result is not None, "Expected recovered result, got None"
    assert isinstance(report, TruncationReport)

    facts = result.get("facts", [])
    assert len(facts) == 3, f"Expected 3 recovered facts, got {len(facts)}"
    assert report.estimated_lost >= 1, f"Expected estimated_lost >= 1, got {report.estimated_lost}"
    assert report.raw_bytes == len(truncated.encode())
    assert report.last_boundary_offset > 0
