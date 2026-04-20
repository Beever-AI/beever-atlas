"""Unit tests for capabilities.sync.trigger_sync cooldown enforcement.

Tests:
- CooldownActive is raised when within the cooldown window
- No exception when cooldown has expired
- ChannelAccessDenied is raised when channel access is denied
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.capabilities.errors import ChannelAccessDenied, CooldownActive
from beever_atlas.capabilities.sync import trigger_sync


def _make_last_job(status: str, completed_minutes_ago: float) -> MagicMock:
    job = MagicMock()
    job.status = status
    job.completed_at = datetime.now(tz=UTC) - timedelta(minutes=completed_minutes_ago)
    return job


@pytest.mark.asyncio
async def test_raises_cooldown_active_within_window():
    """trigger_sync raises CooldownActive when within the cooldown window."""
    last_job = _make_last_job(status="completed", completed_minutes_ago=0.5)

    mock_policy = MagicMock()
    mock_policy.sync.min_sync_interval_minutes = 5
    mock_policy.sync.sync_type = "incremental"

    mock_stores = MagicMock()
    mock_stores.mongodb.get_sync_status = AsyncMock(return_value=last_job)

    with (
        patch("beever_atlas.capabilities.sync.assert_channel_access", return_value=None),
        patch("beever_atlas.capabilities.sync.get_stores", return_value=mock_stores),
        patch(
            "beever_atlas.capabilities.sync.resolve_effective_policy",
            AsyncMock(return_value=mock_policy),
        ),
    ):
        with pytest.raises(CooldownActive) as exc_info:
            await trigger_sync("user-A", "C1")

    # retry_after_seconds should be positive and close to (5 min - 0.5 min) = 270s
    assert exc_info.value.retry_after_seconds > 0
    assert exc_info.value.retry_after_seconds <= 300


@pytest.mark.asyncio
async def test_no_cooldown_when_window_expired():
    """trigger_sync does not raise CooldownActive when cooldown window has passed."""
    last_job = _make_last_job(status="completed", completed_minutes_ago=10)

    mock_policy = MagicMock()
    mock_policy.sync.min_sync_interval_minutes = 5
    mock_policy.sync.sync_type = "incremental"

    mock_stores = MagicMock()
    mock_stores.mongodb.get_sync_status = AsyncMock(return_value=last_job)

    mock_runner = MagicMock()
    mock_runner.start_sync = AsyncMock(return_value="job-123")

    with (
        patch("beever_atlas.capabilities.sync.assert_channel_access", return_value=None),
        patch("beever_atlas.capabilities.sync.get_stores", return_value=mock_stores),
        patch(
            "beever_atlas.capabilities.sync.resolve_effective_policy",
            AsyncMock(return_value=mock_policy),
        ),
        patch("beever_atlas.capabilities.sync.get_sync_runner", return_value=mock_runner),
    ):
        result = await trigger_sync("user-A", "C1")

    assert result["job_id"] == "job-123"
    assert result["status"] == "queued"
    assert "status_uri" in result


@pytest.mark.asyncio
async def test_no_cooldown_when_last_job_failed():
    """trigger_sync skips cooldown enforcement when the last job failed."""
    last_job = _make_last_job(status="failed", completed_minutes_ago=0.5)

    mock_policy = MagicMock()
    mock_policy.sync.min_sync_interval_minutes = 5
    mock_policy.sync.sync_type = "incremental"

    mock_stores = MagicMock()
    mock_stores.mongodb.get_sync_status = AsyncMock(return_value=last_job)

    mock_runner = MagicMock()
    mock_runner.start_sync = AsyncMock(return_value="job-456")

    with (
        patch("beever_atlas.capabilities.sync.assert_channel_access", return_value=None),
        patch("beever_atlas.capabilities.sync.get_stores", return_value=mock_stores),
        patch(
            "beever_atlas.capabilities.sync.resolve_effective_policy",
            AsyncMock(return_value=mock_policy),
        ),
        patch("beever_atlas.capabilities.sync.get_sync_runner", return_value=mock_runner),
    ):
        result = await trigger_sync("user-A", "C1")

    assert result["job_id"] == "job-456"


@pytest.mark.asyncio
async def test_raises_channel_access_denied():
    """trigger_sync raises ChannelAccessDenied when assert_channel_access denies."""
    from fastapi import HTTPException

    with patch(
        "beever_atlas.capabilities.sync.assert_channel_access",
        side_effect=HTTPException(status_code=403, detail="denied"),
    ):
        with pytest.raises(ChannelAccessDenied):
            await trigger_sync("user-A", "C-forbidden")


@pytest.mark.asyncio
async def test_no_cooldown_when_zero_cooldown_policy():
    """trigger_sync does not enforce cooldown when min_sync_interval_minutes is 0."""
    mock_policy = MagicMock()
    mock_policy.sync.min_sync_interval_minutes = 0
    mock_policy.sync.sync_type = "incremental"

    mock_stores = MagicMock()
    mock_stores.mongodb.get_sync_status = AsyncMock(return_value=None)

    mock_runner = MagicMock()
    mock_runner.start_sync = AsyncMock(return_value="job-789")

    with (
        patch("beever_atlas.capabilities.sync.assert_channel_access", return_value=None),
        patch("beever_atlas.capabilities.sync.get_stores", return_value=mock_stores),
        patch(
            "beever_atlas.capabilities.sync.resolve_effective_policy",
            AsyncMock(return_value=mock_policy),
        ),
        patch("beever_atlas.capabilities.sync.get_sync_runner", return_value=mock_runner),
    ):
        result = await trigger_sync("user-A", "C1")

    assert result["job_id"] == "job-789"


@pytest.mark.asyncio
async def test_connection_id_passed_through_to_start_sync():
    """connection_id kwarg reaches SyncRunner.start_sync unchanged.

    Regression guard for the MCP multi-workspace bug (2026-04-20): when
    the caller has two Slack workspaces and MCP triggers sync for a
    channel in the second one, passing connection_id prevents
    SyncRunner._resolve_connection_id from silently falling back to the
    first Slack adapter (channel_not_found).
    """
    mock_policy = MagicMock()
    mock_policy.sync.min_sync_interval_minutes = 0
    mock_policy.sync.sync_type = "incremental"

    mock_stores = MagicMock()
    mock_stores.mongodb.get_sync_status = AsyncMock(return_value=None)

    mock_runner = MagicMock()
    mock_runner.start_sync = AsyncMock(return_value="job-conn-pin")

    with (
        patch("beever_atlas.capabilities.sync.assert_channel_access", return_value=None),
        patch("beever_atlas.capabilities.sync.get_stores", return_value=mock_stores),
        patch(
            "beever_atlas.capabilities.sync.resolve_effective_policy",
            AsyncMock(return_value=mock_policy),
        ),
        patch("beever_atlas.capabilities.sync.get_sync_runner", return_value=mock_runner),
    ):
        result = await trigger_sync(
            "mcp:abc",
            "C0AQCCYA13K",
            connection_id="ead8748a-9183-4174-be7e-ea603bf5d589",
        )

    assert result["job_id"] == "job-conn-pin"
    call_kwargs = mock_runner.start_sync.call_args.kwargs
    assert call_kwargs["connection_id"] == "ead8748a-9183-4174-be7e-ea603bf5d589"
    assert call_kwargs["owner_principal_id"] == "mcp:abc"


@pytest.mark.asyncio
async def test_connection_id_defaults_to_none_when_omitted():
    """When caller omits connection_id, start_sync receives None and
    SyncRunner's existing selected_channels-based resolver kicks in.
    """
    mock_policy = MagicMock()
    mock_policy.sync.min_sync_interval_minutes = 0
    mock_policy.sync.sync_type = "incremental"

    mock_stores = MagicMock()
    mock_stores.mongodb.get_sync_status = AsyncMock(return_value=None)

    mock_runner = MagicMock()
    mock_runner.start_sync = AsyncMock(return_value="job-default")

    with (
        patch("beever_atlas.capabilities.sync.assert_channel_access", return_value=None),
        patch("beever_atlas.capabilities.sync.get_stores", return_value=mock_stores),
        patch(
            "beever_atlas.capabilities.sync.resolve_effective_policy",
            AsyncMock(return_value=mock_policy),
        ),
        patch("beever_atlas.capabilities.sync.get_sync_runner", return_value=mock_runner),
    ):
        await trigger_sync("user-A", "C1")

    assert mock_runner.start_sync.call_args.kwargs["connection_id"] is None
