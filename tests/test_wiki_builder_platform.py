"""Regression tests for WikiBuilder platform detection.

The previous implementation hardcoded ``platform="slack"`` (and, via the
``channel_id and "slack"`` short-circuit, effectively always "slack"). The
fix derives platform from the channel_id format so Discord channels are
labeled correctly in both ``WikiStructure`` and ``WikiResponse``.
"""

from __future__ import annotations

import pytest

from beever_atlas.wiki.builder import _detect_platform


class TestDetectPlatform:
    def test_slack_channel_id(self):
        assert _detect_platform("C0123ABCDE") == "slack"
        assert _detect_platform("D0123ABCDE") == "slack"
        assert _detect_platform("G0123ABCDE") == "slack"

    def test_discord_channel_id(self):
        assert _detect_platform("123456789012345678") == "discord"
        assert _detect_platform("12345678901234567890") == "discord"

    def test_unknown_channel_id(self):
        assert _detect_platform("my-custom-channel") == "unknown"
        assert _detect_platform("example_chat") == "unknown"
        assert _detect_platform("") == "unknown"

    def test_does_not_hardcode_slack(self):
        assert _detect_platform("999888777666555444") != "slack"


@pytest.mark.parametrize(
    ("channel_id", "expected"),
    [
        ("C0ABCDE1234", "slack"),
        ("D9ZYX876543", "slack"),
        ("123456789012345678", "discord"),
        ("some-csv-chat", "unknown"),
    ],
)
def test_platform_detection_is_deterministic(channel_id, expected):
    assert _detect_platform(channel_id) == expected
