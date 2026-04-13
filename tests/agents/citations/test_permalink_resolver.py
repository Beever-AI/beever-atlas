"""Unit tests for the permalink resolver.

Verifies each kind dispatch, platform-specific URL formats, and
graceful-null fallbacks for missing metadata.
"""

from __future__ import annotations

from beever_atlas.agents.citations.permalink_resolver import (
    PermalinkResolver,
    reset_warn_cache,
)
from beever_atlas.agents.citations.types import Source


def _source(kind, native, source_id="src_testtesttest"):
    return Source(
        id=source_id,
        kind=kind,
        title="t",
        excerpt="e",
        retrieved_by={},
        native=native,
    )


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
    r = PermalinkResolver()
    s = _source(
        "channel_message",
        {"platform": "file", "channel_id": "C1", "file_id": "F9"},
    )
    assert r.resolve(s) == "/files/F9"


def test_wiki_page_internal_route():
    r = PermalinkResolver()
    s = _source(
        "wiki_page", {"channel_id": "C1", "page_type": "overview"}
    )
    assert r.resolve(s) == "/channel/C1/wiki/overview"


def test_wiki_page_with_slug_anchor():
    r = PermalinkResolver()
    s = _source(
        "wiki_page",
        {"channel_id": "C1", "page_type": "faq", "slug": "q-12"},
    )
    assert r.resolve(s) == "/channel/C1/wiki/faq#q-12"


def test_qa_history_internal_route():
    r = PermalinkResolver()
    s = _source("qa_history", {"qa_id": "QA1", "session_id": "S1"})
    assert r.resolve(s) == "/ask?session=S1#qa-QA1"


def test_qa_history_no_session():
    r = PermalinkResolver()
    s = _source("qa_history", {"qa_id": "QA1"})
    assert r.resolve(s) == "/ask#qa-QA1"


def test_uploaded_file_route():
    r = PermalinkResolver()
    s = _source("uploaded_file", {"file_id": "F7"})
    assert r.resolve(s) == "/files/F7"


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
