"""Tests proving wiki_tools return dicts with a clean 'title' field.

These tests mock the underlying store/cache calls so they run without
infrastructure. They verify:
- The returned dict has a non-empty 'title' that is NOT the excerpt/summary.
- The title is a short human-readable label, not prose.
- All known page_types produce a recognised label.

Patch targets use the *source* module path because WikiCache, get_settings,
and get_stores are all local imports inside the function bodies — each call
does a fresh `from X import Y`, so we patch the name on the defining module.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# get_wiki_page
# ---------------------------------------------------------------------------


def _make_page(
    page_type: str, content: str = "Some long content here.", summary: str = "Summary text."
) -> dict:
    return {"page_type": page_type, "content": content, "summary": summary}


def _patch_wiki_cache(mock_cache, mock_settings):
    """Return a context-manager stack patching WikiCache and get_settings."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("beever_atlas.wiki.cache.WikiCache", return_value=mock_cache))
    stack.enter_context(patch("beever_atlas.infra.config.get_settings", return_value=mock_settings))
    return stack


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page_type, expected_title",
    [
        ("overview", "Overview"),
        ("faq", "FAQ"),
        ("decisions", "Decisions"),
        ("people", "People"),
        ("glossary", "Glossary"),
        ("activity", "Activity"),
        ("topics", "Topics"),
    ],
)
async def test_get_wiki_page_title_label(page_type, expected_title):
    """get_wiki_page must return a dict with title equal to the nice label."""
    from beever_atlas.agents.tools.wiki_tools import get_wiki_page

    fake_page = _make_page(page_type)

    mock_cache = MagicMock()
    mock_cache.get_page = AsyncMock(return_value=fake_page)

    mock_settings = MagicMock()
    mock_settings.mongodb_uri = "mongodb://localhost:27017"

    with (
        patch("beever_atlas.wiki.cache.WikiCache", return_value=mock_cache),
        patch("beever_atlas.infra.config.get_settings", return_value=mock_settings),
    ):
        result = await get_wiki_page.__wrapped__(channel_id="C1", page_type=page_type)

    assert result is not None
    assert "title" in result, f"title key missing for page_type={page_type}"
    assert result["title"] == expected_title
    # title must NOT be the summary or content body
    assert result["title"] != result.get("summary", ""), "title must not equal summary"
    assert result["title"] != result.get("content", ""), "title must not equal content"
    assert result["title"] != result.get("text", ""), "title must not equal text"


@pytest.mark.asyncio
async def test_get_wiki_page_title_is_short_label():
    """Title must be a short label (< 40 chars), not a prose excerpt."""
    from beever_atlas.agents.tools.wiki_tools import get_wiki_page

    long_summary = "This channel is a lively hub of discussion with many participants. " * 3
    fake_page = _make_page("overview", summary=long_summary)

    mock_cache = MagicMock()
    mock_cache.get_page = AsyncMock(return_value=fake_page)

    mock_settings = MagicMock()
    mock_settings.mongodb_uri = "mongodb://localhost:27017"

    with (
        patch("beever_atlas.wiki.cache.WikiCache", return_value=mock_cache),
        patch("beever_atlas.infra.config.get_settings", return_value=mock_settings),
    ):
        result = await get_wiki_page.__wrapped__(channel_id="C1", page_type="overview")

    assert result is not None
    assert len(result["title"]) < 40, "title should be a short label, not prose"


@pytest.mark.asyncio
async def test_get_wiki_page_activity_fresh_fallback_has_title():
    """Activity fresh-fallback path must also set a title."""
    from beever_atlas.agents.tools.wiki_tools import get_wiki_page

    stale_page = _make_page(
        "activity",
        content="No activity recorded in the last 7 days",
        summary="No activity recorded in the last 7 days",
    )
    fresh_facts = [
        {"timestamp": "2026-01-01T10:00:00Z", "author": "alice", "text": "msg 1"},
        {"timestamp": "2026-01-02T10:00:00Z", "author": "bob", "text": "msg 2"},
    ]

    mock_cache = MagicMock()
    mock_cache.get_page = AsyncMock(return_value=stale_page)

    mock_settings = MagicMock()
    mock_settings.mongodb_uri = "mongodb://localhost:27017"

    with (
        patch("beever_atlas.wiki.cache.WikiCache", return_value=mock_cache),
        patch("beever_atlas.infra.config.get_settings", return_value=mock_settings),
        patch(
            "beever_atlas.agents.tools.memory_tools.get_recent_activity",
            new=AsyncMock(return_value=fresh_facts),
        ),
    ):
        result = await get_wiki_page.__wrapped__(channel_id="C1", page_type="activity")

    assert result is not None
    assert "title" in result
    assert result["title"] == "Activity"
    # title must not be the fresh content lines
    assert result["title"] != result.get("text", "")


# ---------------------------------------------------------------------------
# get_topic_overview — channel-level summary (no topic_name)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_topic_overview_channel_summary_title():
    """Channel-level overview must return title='Overview'."""
    from beever_atlas.agents.tools.wiki_tools import get_topic_overview

    fake_summary = SimpleNamespace(text="Channel summary prose.", cluster_count=3, fact_count=100)

    mock_store = MagicMock()
    mock_store.get_channel_summary = AsyncMock(return_value=fake_summary)

    mock_stores_obj = MagicMock()
    mock_stores_obj.weaviate = mock_store

    with patch("beever_atlas.stores.get_stores", return_value=mock_stores_obj):
        result = await get_topic_overview.__wrapped__(channel_id="C1", topic_name=None)

    assert result is not None
    assert "title" in result
    assert result["title"] == "Overview"
    # title must not equal the summary prose
    assert result["title"] != result["summary"]
    assert result["title"] != result["text"]


# ---------------------------------------------------------------------------
# get_topic_overview — topic cluster (with topic_name)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_topic_overview_topic_cluster_title_from_slug():
    """Topic cluster must return title derived from topic_tags slug, not summary."""
    from beever_atlas.agents.tools.wiki_tools import get_topic_overview

    fake_cluster = SimpleNamespace(
        id="cl-1",
        topic_tags=["payments"],
        summary="Everything about payment flows and invoices.",
        member_count=12,
    )

    mock_store = MagicMock()
    mock_store.list_clusters = AsyncMock(return_value=[fake_cluster])

    mock_stores_obj = MagicMock()
    mock_stores_obj.weaviate = mock_store

    with patch("beever_atlas.stores.get_stores", return_value=mock_stores_obj):
        result = await get_topic_overview.__wrapped__(channel_id="C1", topic_name="payments")

    assert result is not None
    assert "title" in result
    # slug "payments" title-cased → "Payments"
    assert result["title"] == "Payments"
    # title must not be the summary prose
    assert result["title"] != result["summary"]
    assert result["title"] != result["text"]
    assert len(result["title"]) < 40


@pytest.mark.asyncio
async def test_get_topic_overview_topic_cluster_title_from_topic_name_fallback():
    """When topic_tags is empty, title falls back to topic_name title-cased."""
    from beever_atlas.agents.tools.wiki_tools import get_topic_overview

    fake_cluster = SimpleNamespace(
        id="cl-2",
        topic_tags=[],
        summary="Discussion about onboarding flows.",
        member_count=5,
    )

    mock_store = MagicMock()
    mock_store.list_clusters = AsyncMock(return_value=[fake_cluster])

    mock_stores_obj = MagicMock()
    mock_stores_obj.weaviate = mock_store

    with patch("beever_atlas.stores.get_stores", return_value=mock_stores_obj):
        result = await get_topic_overview.__wrapped__(channel_id="C1", topic_name="onboarding")

    assert result is not None
    assert "title" in result
    assert result["title"] == "Onboarding"
    assert result["title"] != result["summary"]
