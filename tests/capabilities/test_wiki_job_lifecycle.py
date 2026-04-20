"""Tests for Fix #3: refresh_wiki must mark sync_jobs row completed / failed.

Verifies that the persisted-job path calls ``complete_sync_job`` on both
success and failure, and that the synthetic-UUID fallback never calls
``complete_sync_job`` (no row to update).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.capabilities.wiki import refresh_wiki
from beever_atlas.models.persistence import SyncJob


def _make_stores(*, create_raises: bool = False, builder_raises: bool = False):
    mock_stores = MagicMock()
    fake_job = SyncJob(
        channel_id="ch-a",
        sync_type="wiki_refresh",
        total_messages=0,
        owner_principal_id="mcp:testhash",
        kind="wiki_refresh",
    )
    if create_raises:
        mock_stores.mongodb.create_sync_job = AsyncMock(
            side_effect=RuntimeError("DB unavailable")
        )
    else:
        mock_stores.mongodb.create_sync_job = AsyncMock(return_value=fake_job)
    mock_stores.mongodb.complete_sync_job = AsyncMock()
    mock_stores.mongodb.get_last_job_by_kind = AsyncMock(return_value=None)

    mock_builder_instance = MagicMock()
    if builder_raises:
        mock_builder_instance.refresh_wiki = AsyncMock(
            side_effect=RuntimeError("builder boom")
        )
    else:
        mock_builder_instance.refresh_wiki = AsyncMock()

    mock_stores.weaviate = MagicMock()
    mock_stores.graph = MagicMock()
    return mock_stores, fake_job, mock_builder_instance


@pytest.mark.asyncio
async def test_success_path_marks_job_completed():
    mock_stores, fake_job, mock_builder_instance = _make_stores()
    mock_cache = MagicMock()
    mock_cache.set_generation_status = AsyncMock()

    captured: list = []

    def _ensure_future(coro):
        captured.append(coro)
        return MagicMock()

    with patch(
        "beever_atlas.capabilities.wiki.assert_channel_access", new=AsyncMock()
    ), patch(
        "beever_atlas.stores.get_stores", return_value=mock_stores
    ), patch(
        "beever_atlas.wiki.cache.WikiCache", return_value=mock_cache
    ), patch(
        "beever_atlas.wiki.builder.WikiBuilder", return_value=mock_builder_instance
    ), patch(
        "asyncio.ensure_future", side_effect=_ensure_future
    ):
        await refresh_wiki("mcp:testhash", "ch-a")
        # Manually drive the captured background coroutine to completion.
        assert len(captured) == 1
        await captured[0]

    mock_stores.mongodb.complete_sync_job.assert_awaited_once_with(
        fake_job.id, status="completed"
    )


@pytest.mark.asyncio
async def test_failure_path_marks_job_failed_with_errors():
    mock_stores, fake_job, mock_builder_instance = _make_stores(builder_raises=True)
    mock_cache = MagicMock()
    mock_cache.set_generation_status = AsyncMock()

    captured: list = []

    def _ensure_future(coro):
        captured.append(coro)
        return MagicMock()

    with patch(
        "beever_atlas.capabilities.wiki.assert_channel_access", new=AsyncMock()
    ), patch(
        "beever_atlas.stores.get_stores", return_value=mock_stores
    ), patch(
        "beever_atlas.wiki.cache.WikiCache", return_value=mock_cache
    ), patch(
        "beever_atlas.wiki.builder.WikiBuilder", return_value=mock_builder_instance
    ), patch(
        "asyncio.ensure_future", side_effect=_ensure_future
    ):
        await refresh_wiki("mcp:testhash", "ch-a")
        await captured[0]

    # complete_sync_job called with status=failed and errors list populated.
    mock_stores.mongodb.complete_sync_job.assert_awaited_once()
    call_kwargs = mock_stores.mongodb.complete_sync_job.call_args.kwargs
    assert call_kwargs.get("status") == "failed"
    assert call_kwargs.get("errors") == ["builder boom"]
    # cache.set_generation_status also called with status=failed.
    mock_cache.set_generation_status.assert_any_await(
        "ch-a", status="failed", stage="error", error="builder boom"
    )


@pytest.mark.asyncio
async def test_synthetic_uuid_fallback_never_calls_complete_sync_job():
    """When create_sync_job raises, is_persisted_job=False, so no
    complete_sync_job call must be made (no row to update)."""
    mock_stores, _, mock_builder_instance = _make_stores(create_raises=True)
    mock_cache = MagicMock()
    mock_cache.set_generation_status = AsyncMock()

    captured: list = []

    def _ensure_future(coro):
        captured.append(coro)
        return MagicMock()

    with patch(
        "beever_atlas.capabilities.wiki.assert_channel_access", new=AsyncMock()
    ), patch(
        "beever_atlas.stores.get_stores", return_value=mock_stores
    ), patch(
        "beever_atlas.wiki.cache.WikiCache", return_value=mock_cache
    ), patch(
        "beever_atlas.wiki.builder.WikiBuilder", return_value=mock_builder_instance
    ), patch(
        "asyncio.ensure_future", side_effect=_ensure_future
    ):
        await refresh_wiki("mcp:testhash", "ch-a")
        await captured[0]

    mock_stores.mongodb.complete_sync_job.assert_not_awaited()
