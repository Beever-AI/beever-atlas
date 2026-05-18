"""RES-287/4a — orphan-channel platform fallback regression tests.

When a channel has no `connection_id` (CSV-imported, pre-connection-model
legacy, etc.) and its channel id doesn't match a known shape, the platform
field used to hardcode-fall-back to ``"discord"`` (api/channels.py) or
``"slack"`` (agents/tools/_citation_decorator.py). That painted the wrong
icon in the sidebar and produced broken Slack-shaped permalinks for
non-Slack data. The truthful answer is ``"unknown"`` — PlatformIcon falls
back to the neutral MessageSquare icon and the permalink resolver returns
``None`` for unknown platforms.
"""

from __future__ import annotations


def test_detect_platform_returns_none_for_arbitrary_string() -> None:
    """The detector's failure mode (returning None) is the trigger for the
    fallback we changed — this asserts the contract the fallback relies on."""
    from beever_atlas.api.channels import _detect_platform_from_channel_id

    # Mattermost channel ids are 26-char base-36; not matched by Slack
    # (starts with C/D/G) or Discord (17-20 digit snowflake) detection.
    assert _detect_platform_from_channel_id("uksaigrf77ywbyyg8ooz1ozqxa") is None
    # CSV-style human ids
    assert _detect_platform_from_channel_id("example_chat") is None
    assert _detect_platform_from_channel_id("any-random-thing") is None


def test_detect_platform_still_recognizes_slack_and_discord() -> None:
    """Sanity: the fallback only fires when detection misses — make sure the
    existing detection paths still work and aren't being shadowed."""
    from beever_atlas.api.channels import _detect_platform_from_channel_id

    assert _detect_platform_from_channel_id("C12345678") == "slack"
    assert _detect_platform_from_channel_id("D12345678") == "slack"
    assert _detect_platform_from_channel_id("G12345678") == "slack"
    # Discord snowflakes are all-digits, 17-20 chars
    assert _detect_platform_from_channel_id("123456789012345678") == "discord"


def test_citation_decorator_falls_back_to_unknown_when_platform_missing() -> None:
    """RES-287/4a — `_derive_native_identity` for ``channel_message`` items
    without a ``platform`` field used to produce a ``slack:...`` identity
    string. The fallback now yields ``unknown:...`` so the permalink resolver
    gracefully returns None instead of constructing a broken Slack URL."""
    from beever_atlas.agents.tools._citation_decorator import _derive_native_identity

    # Platform absent entirely
    native_id = _derive_native_identity(
        "channel_message",
        {"channel_id": "C12345", "message_ts": "1234567890.000100", "fact_id": "f-1"},
    )
    assert native_id is not None
    assert native_id.startswith("unknown:"), native_id

    # Platform present and falsy (empty string) — same fallback path
    native_id_empty = _derive_native_identity(
        "channel_message",
        {"platform": "", "channel_id": "C12345", "message_ts": "1.0", "fact_id": "f-2"},
    )
    assert native_id_empty is not None
    assert native_id_empty.startswith("unknown:"), native_id_empty


def test_citation_decorator_keeps_explicit_platform() -> None:
    """The fallback must only fire when ``platform`` is missing/empty — an
    explicit value (mattermost, discord, etc.) must round-trip unchanged."""
    from beever_atlas.agents.tools._citation_decorator import _derive_native_identity

    native = _derive_native_identity(
        "channel_message",
        {
            "platform": "mattermost",
            "channel_id": "uksaigrf77ywbyyg8ooz1ozqxa",
            "message_ts": "2026-05-16T18:00:00",
            "fact_id": "f-99",
        },
    )
    assert native is not None
    assert native.startswith("mattermost:"), native
