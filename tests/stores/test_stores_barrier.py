"""Tests for the `_stores_ready` startup barrier (issue #36).

Covers `wait_for_stores_ready()` semantics and the existing
`get_stores()` fail-fast behavior.

No `@pytest.mark.asyncio` decorators per project convention
(`pyproject.toml` sets `asyncio_mode = "auto"`).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import beever_atlas.stores as stores_mod
from beever_atlas.stores import (
    StoreClients,
    _reset_stores_for_tests,
    get_stores,
    init_stores,
    wait_for_stores_ready,
)


@pytest.fixture
def reset_stores():
    """Per-test reset of the singleton + barrier event.

    The autouse `_init_stores_for_tests` fixture in `tests/conftest.py`
    runs FIRST and may have populated the singleton; we explicitly clear
    it for tests in this module that need to observe the uninitialized
    state.
    """
    saved = stores_mod._stores
    _reset_stores_for_tests()
    yield
    stores_mod._stores = saved


def _mock_clients() -> StoreClients:
    """Mock StoreClients shaped enough for identity comparison."""
    return MagicMock(spec=StoreClients)


# ── (a) existing fail-fast preserved ────────────────────────────────────


def test_get_stores_raises_when_not_initialized(reset_stores) -> None:
    with pytest.raises(RuntimeError, match="Stores not initialized"):
        get_stores()


# ── (b) barrier blocks then unblocks on init ────────────────────────────


async def test_wait_for_stores_ready_blocks_until_init(reset_stores) -> None:
    completed = asyncio.Event()

    async def waiter() -> None:
        await wait_for_stores_ready(timeout=None)
        completed.set()

    task = asyncio.create_task(waiter())

    # Give the waiter a chance to start; assert it has NOT completed yet.
    await asyncio.sleep(0.01)
    assert not completed.is_set(), "barrier must not release before init_stores"
    assert not task.done()

    # Init the singleton — barrier releases.
    init_stores(_mock_clients())

    # Waiter completes promptly (sub-second timeout proves promptness).
    await asyncio.wait_for(task, timeout=1.0)
    assert completed.is_set()


# ── (c) returns immediately when already init ───────────────────────────


async def test_wait_for_stores_ready_returns_immediately_when_already_init() -> None:
    # The autouse fixture has already initialized stores; the event is set.
    # Use a tight timeout to prove the call doesn't block.
    await asyncio.wait_for(wait_for_stores_ready(), timeout=0.05)


# ── (d) multiple concurrent waiters all unblock ─────────────────────────


async def test_multiple_concurrent_waiters_all_unblock(reset_stores) -> None:
    waiters = [asyncio.create_task(wait_for_stores_ready(timeout=None)) for _ in range(5)]
    await asyncio.sleep(0.01)
    assert not any(w.done() for w in waiters), "no waiter should complete before init"

    init_stores(_mock_clients())

    await asyncio.wait_for(asyncio.gather(*waiters), timeout=1.0)
    assert all(w.done() and w.exception() is None for w in waiters)


# ── (e) reset clears event ──────────────────────────────────────────────


def test_reset_stores_for_tests_clears_event() -> None:
    init_stores(_mock_clients())
    assert stores_mod._stores_ready.is_set()
    _reset_stores_for_tests()
    assert not stores_mod._stores_ready.is_set()
    with pytest.raises(RuntimeError, match="Stores not initialized"):
        get_stores()


# ── (f) timeout raises diagnostic RuntimeError ──────────────────────────


async def test_wait_for_stores_ready_times_out_when_never_init(reset_stores) -> None:
    with pytest.raises(RuntimeError, match=r"Stores not initialized within 0\.05s"):
        await wait_for_stores_ready(timeout=0.05)


# ── (g) re-init logs WARNING and overwrites ─────────────────────────────


def test_init_stores_warns_on_reinit(reset_stores, monkeypatch) -> None:
    """The autouse `_auth_bypass` fixture imports `beever_atlas.server.app`,
    which sets `propagate=False` on the `beever_atlas` logger — so caplog
    (root-level) doesn't see records from `beever_atlas.stores`. Capture
    the WARNING via direct monkeypatch on `stores_mod.logger.warning`.
    """
    captured: list[str] = []
    monkeypatch.setattr(
        stores_mod.logger,
        "warning",
        lambda msg, *a, **kw: captured.append(msg % a if a else msg),
    )

    first = _mock_clients()
    second = _mock_clients()
    init_stores(first)
    init_stores(second)

    assert any("init_stores called while _stores is already set" in m for m in captured), (
        f"expected re-init WARNING; got: {captured}"
    )
    assert get_stores() is second, "re-init must overwrite the singleton"
