"""Unit tests for the permalink resolver.

Verifies each kind dispatch, platform-specific URL formats, and
graceful-null fallbacks for missing metadata.
"""

from __future__ import annotations

import pytest

from beever_atlas.agents.citations.permalink_resolver import (
    PermalinkResolver,
    reset_warn_cache,
)
from beever_atlas.agents.citations.types import Source
from beever_atlas.infra.config import get_settings

# Public web base URL used by the internal-route tests so wiki/qa/file
# permalinks resolve to ABSOLUTE links. Channel-message permalinks are already
# absolute and ignore this entirely.
_BASE = "https://atlas.example.com"


def _source(kind, native, source_id="src_testtesttest"):
    return Source(
        id=source_id,
        kind=kind,
        title="t",
        excerpt="e",
        retrieved_by={},
        native=native,
    )


@pytest.fixture(autouse=True)
def _public_web_url(monkeypatch):
    """Default the internal-route base URL ON for this module and reset the
    cached Settings so each test sees a fresh value. Tests that need the
    'unset' behaviour delete the env var and clear the cache themselves.
    """
    monkeypatch.setenv("PUBLIC_WEB_URL", _BASE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def setup_function():
    reset_warn_cache()


def test_slack_permalink():
    r = PermalinkResolver()
    s = _source(
        "channel_message",
        {
            "platform": "slack",
            "channel_id": "C08TX",
            "message_ts": "1712500000.001100",
            "workspace_domain": "beever",
        },
    )
    expected = "https://beever.slack.com/archives/C08TX/p1712500000001100"
    assert r.resolve(s) == expected


def test_slack_permalink_from_native_message_id_when_ts_is_iso():
    """Real Slack facts store message_ts as an ISO datetime (display format); the
    numeric ts lives on the native message id. The resolver must fall back to it."""
    r = PermalinkResolver()
    s = _source(
        "channel_message",
        {
            "platform": "slack",
            "channel_id": "C0B5YCR1NL8",
            "message_ts": "2026-05-21T19:14:45.369000+00:00",  # ISO — not usable directly
            "message_id": "1779390885.369099",  # the real numeric Slack ts
            "workspace_domain": "beeveratlas",
        },
    )
    expected = "https://beeveratlas.slack.com/archives/C0B5YCR1NL8/p1779390885369099"
    assert r.resolve(s) == expected


def test_slack_iso_ts_and_no_native_id_returns_null():
    """An ISO ts with no numeric native id can't build a permalink — returns None,
    never a broken URL."""
    r = PermalinkResolver()
    s = _source(
        "channel_message",
        {
            "platform": "slack",
            "channel_id": "C1",
            "message_ts": "2026-05-21T19:14:45+00:00",
            "workspace_domain": "w",
        },
    )
    assert r.resolve(s) is None


def test_slack_missing_workspace_returns_null():
    r = PermalinkResolver()
    s = _source(
        "channel_message",
        {
            "platform": "slack",
            "channel_id": "C1",
            "message_ts": "1712500000.001100",
        },
    )
    assert r.resolve(s) is None


def test_slack_missing_ts_returns_null():
    r = PermalinkResolver()
    s = _source(
        "channel_message",
        {"platform": "slack", "channel_id": "C1", "workspace_domain": "w"},
    )
    assert r.resolve(s) is None


def test_discord_permalink():
    r = PermalinkResolver()
    s = _source(
        "channel_message",
        {
            "platform": "discord",
            "channel_id": "111",
            "guild_id": "222",
            "message_id": "333",
        },
    )
    assert r.resolve(s) == "https://discord.com/channels/222/111/333"


def test_teams_permalink():
    r = PermalinkResolver()
    s = _source(
        "channel_message",
        {"platform": "teams", "channel_id": "C1", "message_id": "M1"},
    )
    assert r.resolve(s) == "https://teams.microsoft.com/l/message/C1/M1"


def test_missing_platform_returns_null():
    r = PermalinkResolver()
    s = _source("channel_message", {"channel_id": "C1", "message_ts": "1.1"})
    assert r.resolve(s) is None


def test_unknown_platform_returns_null():
    r = PermalinkResolver()
    s = _source("channel_message", {"platform": "irc", "channel_id": "C"})
    assert r.resolve(s) is None


def test_file_platform_falls_back_to_internal_route():
    """A file-platform channel_message permalink is absolutized through
    PUBLIC_WEB_URL (same as _resolve_uploaded_file) so the renderer keeps it —
    a bare relative /files/{id} would be dropped by cleanUrl."""
    r = PermalinkResolver()
    s = _source(
        "channel_message",
        {"platform": "file", "channel_id": "C1", "file_id": "F9"},
    )
    assert r.resolve(s) == f"{_BASE}/files/F9"


def test_file_platform_none_when_base_url_unset(monkeypatch):
    """With PUBLIC_WEB_URL unset the file-platform route resolves to None rather
    than a broken bare relative path (consistent with the other internal kinds)."""
    monkeypatch.delenv("PUBLIC_WEB_URL", raising=False)
    get_settings.cache_clear()
    r = PermalinkResolver()
    s = _source(
        "channel_message",
        {"platform": "file", "channel_id": "C1", "file_id": "F9"},
    )
    assert r.resolve(s) is None


def test_wiki_page_internal_route():
    r = PermalinkResolver()
    s = _source("wiki_page", {"channel_id": "C1", "page_type": "overview"})
    assert r.resolve(s) == f"{_BASE}/channel/C1/wiki/overview"


def test_wiki_page_with_slug_anchor():
    r = PermalinkResolver()
    s = _source(
        "wiki_page",
        {"channel_id": "C1", "page_type": "faq", "slug": "q-12"},
    )
    assert r.resolve(s) == f"{_BASE}/channel/C1/wiki/faq#q-12"


def test_wiki_page_absolute_when_base_url_set():
    """A wiki permalink is an ABSOLUTE http(s) link when PUBLIC_WEB_URL is set,
    so the chat renderer's cleanUrl keeps it (it drops bare relative paths)."""
    r = PermalinkResolver()
    s = _source("wiki_page", {"channel_id": "C1", "page_type": "overview"})
    url = r.resolve(s)
    assert url is not None
    assert url.startswith("https://")
    assert url == f"{_BASE}/channel/C1/wiki/overview"


def test_wiki_page_none_when_base_url_unset(monkeypatch):
    """With PUBLIC_WEB_URL unset, the resolver returns None for internal routes
    rather than emitting a broken bare relative path."""
    monkeypatch.delenv("PUBLIC_WEB_URL", raising=False)
    get_settings.cache_clear()
    r = PermalinkResolver()
    s = _source("wiki_page", {"channel_id": "C1", "page_type": "overview"})
    assert r.resolve(s) is None


def test_qa_history_internal_route():
    r = PermalinkResolver()
    s = _source("qa_history", {"qa_id": "QA1", "session_id": "S1"})
    assert r.resolve(s) == f"{_BASE}/ask?session=S1#qa-QA1"


def test_qa_history_no_session():
    r = PermalinkResolver()
    s = _source("qa_history", {"qa_id": "QA1"})
    assert r.resolve(s) == f"{_BASE}/ask#qa-QA1"


def test_qa_history_none_when_base_url_unset(monkeypatch):
    monkeypatch.delenv("PUBLIC_WEB_URL", raising=False)
    get_settings.cache_clear()
    r = PermalinkResolver()
    s = _source("qa_history", {"qa_id": "QA1"})
    assert r.resolve(s) is None


def test_uploaded_file_route():
    r = PermalinkResolver()
    s = _source("uploaded_file", {"file_id": "F7"})
    assert r.resolve(s) == f"{_BASE}/files/F7"


def test_uploaded_file_none_when_base_url_unset(monkeypatch):
    monkeypatch.delenv("PUBLIC_WEB_URL", raising=False)
    get_settings.cache_clear()
    r = PermalinkResolver()
    s = _source("uploaded_file", {"file_id": "F7"})
    assert r.resolve(s) is None


def test_web_result_passthrough():
    r = PermalinkResolver()
    s = _source("web_result", {"url": "https://example.com/a"})
    assert r.resolve(s) == "https://example.com/a"


def test_web_result_invalid_url_null():
    r = PermalinkResolver()
    s = _source("web_result", {"url": "not-a-url"})
    assert r.resolve(s) is None


def test_graph_relationship_null_by_design():
    r = PermalinkResolver()
    s = _source("graph_relationship", {"subject_id": "s"})
    assert r.resolve(s) is None


def test_resolver_never_throws_on_bad_data():
    r = PermalinkResolver()
    s = _source("channel_message", {"platform": "slack", "channel_id": None, "message_ts": None})
    # Should not raise
    assert r.resolve(s) is None


# ---- table-driven channel_message coverage ----------------------------


@pytest.mark.parametrize(
    "native,expected",
    [
        # Slack: full native → archives permalink
        (
            {
                "platform": "slack",
                "channel_id": "C08TX",
                "message_ts": "1712500000.001100",
                "workspace_domain": "beever",
            },
            "https://beever.slack.com/archives/C08TX/p1712500000001100",
        ),
        # Discord: full native → discord deep link
        (
            {
                "platform": "discord",
                "channel_id": "111",
                "guild_id": "222",
                "message_id": "333",
            },
            "https://discord.com/channels/222/111/333",
        ),
        # Teams: channel + message id → teams deep link
        (
            {"platform": "teams", "channel_id": "C1", "message_id": "M1"},
            "https://teams.microsoft.com/l/message/C1/M1",
        ),
        # Missing native (no platform) → None
        ({"channel_id": "C1", "message_ts": "1.1"}, None),
        # Slack missing workspace_domain → None (never a broken URL)
        (
            {"platform": "slack", "channel_id": "C1", "message_ts": "1712500000.001100"},
            None,
        ),
        # Discord missing message_id → None
        ({"platform": "discord", "channel_id": "111", "guild_id": "222"}, None),
        # Teams missing message_id → None
        ({"platform": "teams", "channel_id": "C1"}, None),
        # Empty native → None
        ({}, None),
    ],
)
def test_channel_message_table(native, expected):
    reset_warn_cache()
    r = PermalinkResolver()
    s = _source("channel_message", native)
    assert r.resolve(s) == expected
