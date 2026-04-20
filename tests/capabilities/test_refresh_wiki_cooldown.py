"""Regression tests for ``capabilities.wiki.refresh_wiki`` cooldown enforcement.

Addresses code-review finding H4: ``refresh_wiki`` previously had no
cooldown, so a caller could burn the rate-limit window on 5 concurrent
LLM-heavy wiki regenerations per minute. The capability now enforces a
5-minute cooldown mirroring ``capabilities/sync.trigger_sync``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.capabilities.errors import CooldownActive
from beever_atlas.capabilities.wiki import refresh_wiki


def _make_last_wiki_job(status: str, completed_minutes_ago: float) -> SimpleNamespace:
    return SimpleNamespace(
        kind="wiki_refresh",
        status=status,
        completed_at=datetime.now(tz=UTC) - timedelta(minutes=completed_minutes_ago),
    )


@pytest.mark.asyncio
async def test_raises_cooldown_active_within_window():
    """refresh_wiki raises CooldownActive when last completion was <5 min ago."""
    last_job = _make_last_wiki_job("completed", completed_minutes_ago=1.0)

    mock_stores = MagicMock()
    mock_stores.mongodb.get_last_job_by_kind = AsyncMock(return_value=last_job)

    with patch(
        "beever_atlas.capabilities.wiki.assert_channel_access",
        new=AsyncMock(return_value=None),
    ), patch(
        "beever_atlas.stores.get_stores", return_value=mock_stores
    ):
        with pytest.raises(CooldownActive) as exc_info:
            await refresh_wiki("mcp:alice", "ch-a")

    assert exc_info.value.retry_after_seconds > 0
    # With 1 min elapsed of a 5 min window, retry_after ≈ 240s.
    assert 200 <= exc_info.value.retry_after_seconds <= 300


@pytest.mark.asyncio
async def test_no_cooldown_when_window_expired():
    """A wiki_refresh that completed >5 min ago does NOT trigger cooldown."""
    last_job = _make_last_wiki_job("completed", completed_minutes_ago=10)

    mock_stores = MagicMock()
    mock_stores.mongodb.get_last_job_by_kind = AsyncMock(return_value=last_job)
    mock_stores.mongodb.create_sync_job = AsyncMock(return_value=SimpleNamespace(id="job-new"))
    mock_stores.weaviate = MagicMock()
    mock_stores.graph = MagicMock()

    with patch(
        "beever_atlas.capabilities.wiki.assert_channel_access",
        new=AsyncMock(return_value=None),
    ), patch(
        "beever_atlas.stores.get_stores", return_value=mock_stores
    ), patch(
        "beever_atlas.wiki.cache.WikiCache"
    ), patch(
        "asyncio.ensure_future"
    ):
        result = await refresh_wiki("mcp:alice", "ch-a")

    assert result["job_id"] == "job-new"
    assert result["status"] == "queued"


@pytest.mark.asyncio
async def test_no_cooldown_when_last_job_was_sync_not_wiki():
    """A recent *sync* job should NOT trigger the wiki cooldown — the
    cooldown is kind-specific to ``wiki_refresh`` (Fix #4).

    Under the new ``get_last_job_by_kind`` store helper the filter happens
    at the query boundary, so this test asserts the store returns ``None``
    when no ``wiki_refresh`` job has ever run and a ``sync`` job is
    therefore ignored.
    """
    mock_stores = MagicMock()
    mock_stores.mongodb.get_last_job_by_kind = AsyncMock(return_value=None)
    mock_stores.mongodb.create_sync_job = AsyncMock(return_value=SimpleNamespace(id="job-x"))
    mock_stores.weaviate = MagicMock()
    mock_stores.graph = MagicMock()

    with patch(
        "beever_atlas.capabilities.wiki.assert_channel_access",
        new=AsyncMock(return_value=None),
    ), patch(
        "beever_atlas.stores.get_stores", return_value=mock_stores
    ), patch(
        "beever_atlas.wiki.cache.WikiCache"
    ), patch(
        "asyncio.ensure_future"
    ):
        # Should NOT raise — the recent job is kind=sync, not wiki_refresh.
        result = await refresh_wiki("mcp:alice", "ch-a")

    assert result["status"] == "queued"


@pytest.mark.asyncio
async def test_no_cooldown_when_last_job_still_running():
    """A still-running wiki job does not trigger cooldown — rate limiter
    upstream caps concurrency, and callers may legitimately poll."""
    running = SimpleNamespace(
        kind="wiki_refresh", status="running", completed_at=None
    )
    mock_stores = MagicMock()
    mock_stores.mongodb.get_sync_status = AsyncMock(return_value=running)
    mock_stores.mongodb.create_sync_job = AsyncMock(return_value=SimpleNamespace(id="job-y"))
    mock_stores.weaviate = MagicMock()
    mock_stores.graph = MagicMock()

    with patch(
        "beever_atlas.capabilities.wiki.assert_channel_access",
        new=AsyncMock(return_value=None),
    ), patch(
        "beever_atlas.stores.get_stores", return_value=mock_stores
    ), patch(
        "beever_atlas.wiki.cache.WikiCache"
    ), patch(
        "asyncio.ensure_future"
    ):
        result = await refresh_wiki("mcp:alice", "ch-a")

    assert result["status"] == "queued"
