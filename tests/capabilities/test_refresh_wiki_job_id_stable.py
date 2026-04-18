"""Regression test: refresh_wiki returns a job_id that matches the persisted sync_jobs row.

Phase 1 bug: refresh_wiki generated a synthetic uuid that was returned in the
response but NOT the id stored in sync_jobs. This meant atlas://job/<returned_id>
could never resolve via get_job_status.

Fix (Phase 4): capture the SyncJob returned by create_sync_job and use job.id.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.capabilities.wiki import refresh_wiki
from beever_atlas.models.persistence import SyncJob


@pytest.mark.asyncio
async def test_refresh_wiki_job_id_matches_persisted_row():
    """The job_id returned by refresh_wiki equals the id of the stored SyncJob."""
    # Build a fake SyncJob with a known id (simulates what create_sync_job returns).
    fake_job = SyncJob(
        channel_id="ch-test",
        sync_type="wiki_refresh",
        total_messages=0,
        owner_principal_id="mcp:testhash",
        kind="wiki_refresh",
    )
    persisted_id = fake_job.id  # the auto-generated uuid

    mock_stores = MagicMock()
    mock_stores.mongodb.create_sync_job = AsyncMock(return_value=fake_job)

    mock_cache = MagicMock()
    mock_cache.set_generation_status = AsyncMock()

    # Patch assert_channel_access to succeed silently.
    with patch(
        "beever_atlas.capabilities.wiki.assert_channel_access",
        new=AsyncMock(),
    ), patch(
        "beever_atlas.stores.get_stores",
        return_value=mock_stores,
    ), patch(
        "beever_atlas.wiki.cache.WikiCache",
        return_value=mock_cache,
    ), patch(
        "beever_atlas.infra.config.get_settings",
        return_value=MagicMock(mongodb_uri="mongodb://localhost:27017"),
    ), patch(
        "asyncio.ensure_future",
    ):
        result = await refresh_wiki("mcp:testhash", "ch-test")

    assert result["job_id"] == persisted_id, (
        f"refresh_wiki returned job_id={result['job_id']!r} but "
        f"the persisted SyncJob has id={persisted_id!r}. "
        "The atlas://job/<returned_id> resource would be unresolvable."
    )
    assert result["status_uri"] == f"atlas://job/{persisted_id}"
    assert result["status"] == "queued"


@pytest.mark.asyncio
async def test_refresh_wiki_fallback_job_id_when_db_fails():
    """When create_sync_job raises, refresh_wiki falls back to a synthetic uuid and still returns."""
    mock_stores = MagicMock()
    mock_stores.mongodb.create_sync_job = AsyncMock(
        side_effect=RuntimeError("DB unavailable")
    )

    mock_cache = MagicMock()
    mock_cache.set_generation_status = AsyncMock()

    with patch(
        "beever_atlas.capabilities.wiki.assert_channel_access",
        new=AsyncMock(),
    ), patch(
        "beever_atlas.stores.get_stores",
        return_value=mock_stores,
    ), patch(
        "beever_atlas.wiki.cache.WikiCache",
        return_value=mock_cache,
    ), patch(
        "beever_atlas.infra.config.get_settings",
        return_value=MagicMock(mongodb_uri="mongodb://localhost:27017"),
    ), patch(
        "asyncio.ensure_future",
    ):
        result = await refresh_wiki("mcp:testhash", "ch-fallback")

    # Even on DB failure, a job_id must be returned (synthetic uuid).
    assert "job_id" in result
    assert result["job_id"]  # non-empty string
    assert result["status_uri"] == f"atlas://job/{result['job_id']}"
    assert result["status"] == "queued"
