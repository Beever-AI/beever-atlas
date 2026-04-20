"""Tests for Fix #4: wiki cooldown filters by ``kind="wiki_refresh"``.

Confirms that a recent completed ``sync`` job does not trigger the
wiki-specific cooldown and that ``get_last_job_by_kind`` is the store
method actually called.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.capabilities.errors import CooldownActive
from beever_atlas.capabilities.wiki import refresh_wiki


def _make_wiki_job(status: str, completed_minutes_ago: float) -> SimpleNamespace:
    return SimpleNamespace(
        kind="wiki_refresh",
        status=status,
        completed_at=datetime.now(tz=UTC) - timedelta(minutes=completed_minutes_ago),
    )


@pytest.mark.asyncio
async def test_store_called_with_kind_filter():
    """refresh_wiki must query ``get_last_job_by_kind(..., 'wiki_refresh')``."""
    mock_stores = MagicMock()
    mock_stores.mongodb.get_last_job_by_kind = AsyncMock(return_value=None)
    mock_stores.mongodb.create_sync_job = AsyncMock(return_value=SimpleNamespace(id="job-k"))
    mock_stores.weaviate = MagicMock()
    mock_stores.graph = MagicMock()

    with (
        patch("beever_atlas.capabilities.wiki.assert_channel_access", new=AsyncMock()),
        patch("beever_atlas.stores.get_stores", return_value=mock_stores),
        patch("beever_atlas.wiki.cache.WikiCache"),
        patch("asyncio.ensure_future"),
    ):
        await refresh_wiki("mcp:alice", "ch-a")

    mock_stores.mongodb.get_last_job_by_kind.assert_awaited_once_with("ch-a", "wiki_refresh")


@pytest.mark.asyncio
async def test_recent_wiki_refresh_triggers_cooldown():
    """A completed wiki_refresh within 5 min does trigger cooldown."""
    mock_stores = MagicMock()
    mock_stores.mongodb.get_last_job_by_kind = AsyncMock(
        return_value=_make_wiki_job("completed", completed_minutes_ago=2.0)
    )

    with (
        patch("beever_atlas.capabilities.wiki.assert_channel_access", new=AsyncMock()),
        patch("beever_atlas.stores.get_stores", return_value=mock_stores),
        patch("beever_atlas.wiki.cache.WikiCache"),
    ):
        with pytest.raises(CooldownActive):
            await refresh_wiki("mcp:alice", "ch-a")


@pytest.mark.asyncio
async def test_wiki_refresh_older_than_window_no_cooldown():
    """A completed wiki_refresh older than 5 min does NOT trigger cooldown."""
    mock_stores = MagicMock()
    mock_stores.mongodb.get_last_job_by_kind = AsyncMock(
        return_value=_make_wiki_job("completed", completed_minutes_ago=10.0)
    )
    mock_stores.mongodb.create_sync_job = AsyncMock(return_value=SimpleNamespace(id="job-fresh"))
    mock_stores.weaviate = MagicMock()
    mock_stores.graph = MagicMock()

    with (
        patch("beever_atlas.capabilities.wiki.assert_channel_access", new=AsyncMock()),
        patch("beever_atlas.stores.get_stores", return_value=mock_stores),
        patch("beever_atlas.wiki.cache.WikiCache"),
        patch("asyncio.ensure_future"),
    ):
        result = await refresh_wiki("mcp:alice", "ch-a")

    assert result["status"] == "queued"


@pytest.mark.asyncio
async def test_no_prior_wiki_job_no_cooldown_even_with_recent_sync():
    """Even if a sync job recently completed, the wiki cooldown does not
    trigger when no wiki_refresh history exists — because the kind-filtered
    query returns None.
    """
    mock_stores = MagicMock()
    # Store returns None because no wiki_refresh has ever run on this channel.
    mock_stores.mongodb.get_last_job_by_kind = AsyncMock(return_value=None)
    mock_stores.mongodb.create_sync_job = AsyncMock(return_value=SimpleNamespace(id="job-nf"))
    mock_stores.weaviate = MagicMock()
    mock_stores.graph = MagicMock()

    with (
        patch("beever_atlas.capabilities.wiki.assert_channel_access", new=AsyncMock()),
        patch("beever_atlas.stores.get_stores", return_value=mock_stores),
        patch("beever_atlas.wiki.cache.WikiCache"),
        patch("asyncio.ensure_future"),
    ):
        result = await refresh_wiki("mcp:alice", "ch-a")

    assert result["status"] == "queued"
