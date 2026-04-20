"""Regression tests for WS-B2: scheduler semaphore over-release on timeout.

Before the fix, `_execute_sync` always called `release_sync_semaphore()` in
the `finally` block even when `asyncio.wait_for(sem.acquire(), 30)` timed
out and never actually acquired the slot. That corrupted the counter: every
timeout silently incremented capacity by one.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_timeout_does_not_release_semaphore() -> None:
    from beever_atlas.services.scheduler import SyncScheduler

    scheduler = SyncScheduler.__new__(SyncScheduler)
    scheduler._global_semaphore = asyncio.Semaphore(1)
    # Saturate: the next acquire call will block until timeout.
    await scheduler._global_semaphore.acquire()

    effective = type(
        "Eff",
        (),
        {
            "sync": type("S", (), {"min_sync_interval_minutes": 0, "sync_type": "auto"})(),
        },
    )()

    fake_stores = type("Stores", (), {})()
    fake_stores.mongodb = type("M", (), {})()
    fake_stores.mongodb.get_sync_status = AsyncMock(return_value=None)

    with (
        patch(
            "beever_atlas.services.policy_resolver.resolve_effective_policy",
            AsyncMock(return_value=effective),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch("asyncio.wait_for", AsyncMock(side_effect=asyncio.TimeoutError())),
    ):
        await scheduler._execute_sync("channel-1")

    # The semaphore value must NOT have been incremented by the finally clause.
    # Since capacity is 1 and we already acquired it, value should still be 0.
    assert scheduler._global_semaphore._value == 0, (
        "Semaphore counter was over-released on timeout path"
    )


@pytest.mark.asyncio
async def test_successful_acquire_still_releases() -> None:
    from beever_atlas.services.scheduler import SyncScheduler

    scheduler = SyncScheduler.__new__(SyncScheduler)
    scheduler._global_semaphore = asyncio.Semaphore(1)

    effective = type(
        "Eff",
        (),
        {
            "sync": type("S", (), {"min_sync_interval_minutes": 0, "sync_type": "auto"})(),
        },
    )()

    fake_stores = type("Stores", (), {})()
    fake_stores.mongodb = type("M", (), {})()
    fake_stores.mongodb.get_sync_status = AsyncMock(return_value=None)

    fake_runner = type("R", (), {})()
    fake_runner.start_sync = AsyncMock(return_value="job-1")

    with (
        patch(
            "beever_atlas.services.policy_resolver.resolve_effective_policy",
            AsyncMock(return_value=effective),
        ),
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch("beever_atlas.api.sync.get_sync_runner", return_value=fake_runner),
    ):
        await scheduler._execute_sync("channel-1")

    # Happy path: acquired then released → value back to full capacity.
    assert scheduler._global_semaphore._value == 1
