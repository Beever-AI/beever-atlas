"""Regression test for WS-B2: semaphore timeout must not leak a permit.

When ``asyncio.wait_for(_global_semaphore.acquire(), timeout=...)`` raises
``TimeoutError``, the semaphore's internal counter must be unchanged because
the acquire never completed. This guards the ``_execute_sync`` path in
``SyncScheduler`` from slowly starving itself if many acquires time out.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.services.scheduler import SyncScheduler


@pytest.mark.asyncio
async def test_execute_sync_timeout_does_not_leak_semaphore_permit():
    scheduler = SyncScheduler.__new__(SyncScheduler)
    scheduler._global_semaphore = asyncio.Semaphore(2)
    scheduler._started = True

    # Capture the initial permit count via a probe.
    sem = scheduler._global_semaphore
    initial_value = sem._value  # noqa: SLF001 - asserting on internal counter

    mock_stores = MagicMock()
    mock_stores.mongodb.get_sync_status = AsyncMock(return_value=None)

    # Make resolve_effective_policy return a trivial policy with cooldown=0 so
    # we go straight to the semaphore acquire path.
    mock_policy = MagicMock()
    mock_policy.sync.min_sync_interval_minutes = 0
    mock_policy.sync.sync_type = "auto"

    async def _raise_timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError

    with (
        patch(
            "beever_atlas.services.policy_resolver.resolve_effective_policy",
            new_callable=AsyncMock,
            return_value=mock_policy,
        ),
        patch(
            "beever_atlas.services.scheduler.asyncio.wait_for",
            side_effect=_raise_timeout,
        ),
        patch("beever_atlas.stores.get_stores", return_value=mock_stores),
    ):
        # Should swallow the TimeoutError internally (see except clause).
        await scheduler._execute_sync("C1")

    # Permit count MUST be unchanged — the acquire never succeeded.
    assert sem._value == initial_value  # noqa: SLF001
