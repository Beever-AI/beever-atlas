"""Tests for the SSE heartbeat that fires while LiteLLM-routed models
(Ollama, GLM, …) are silent before producing their first token.
"""

from __future__ import annotations

import asyncio

import pytest

from beever_atlas.api.ask import _HeartbeatSentinel, _stream_with_heartbeats


async def _async_iter(items, *, delay_per_item: float = 0.0):
    """Yield ``items`` from an async generator with optional inter-item delay."""
    for it in items:
        if delay_per_item:
            await asyncio.sleep(delay_per_item)
        yield it


@pytest.mark.asyncio
async def test_no_heartbeat_when_stream_is_fast():
    """A fast stream — every item arrives within the interval — produces
    zero heartbeats."""
    source = _async_iter(["a", "b", "c"])  # no delay
    result: list = []
    async for ev in _stream_with_heartbeats(source, interval_seconds=10.0):
        result.append(ev)
    assert result == ["a", "b", "c"]
    assert not any(isinstance(r, _HeartbeatSentinel) for r in result)


@pytest.mark.asyncio
async def test_heartbeat_fires_during_silence():
    """When the underlying stream takes longer than the interval to emit
    its next item, a heartbeat lands first."""

    async def _slow():
        await asyncio.sleep(0.25)
        yield "finally"

    result: list = []
    # Run to completion — the stream ends naturally after yielding "finally"
    # so no infinite-loop safeguard is needed. (A buggy implementation that
    # never closes will time out via pytest-asyncio's default test timeout.)
    async for ev in _stream_with_heartbeats(_slow(), interval_seconds=0.05):
        result.append(ev)

    # Several heartbeats followed by the real item
    heartbeats = [r for r in result if isinstance(r, _HeartbeatSentinel)]
    real_events = [r for r in result if not isinstance(r, _HeartbeatSentinel)]
    assert len(heartbeats) >= 1, f"expected ≥1 heartbeat, got {result}"
    assert real_events == ["finally"]
    # elapsed_ms increases monotonically
    elapsed = [h.elapsed_ms for h in heartbeats]
    assert elapsed == sorted(elapsed)


@pytest.mark.asyncio
async def test_heartbeat_stops_after_real_event():
    """Once the first real event arrives, subsequent events are passed
    through immediately even if the underlying stream is fast — no extra
    heartbeats inserted between real items."""

    async def _mixed():
        # First item slow → triggers heartbeat. Then several fast items.
        await asyncio.sleep(0.1)
        yield "first"
        for x in ["b", "c", "d"]:
            yield x

    result: list = []
    async for ev in _stream_with_heartbeats(_mixed(), interval_seconds=0.05):
        result.append(ev)

    # After the first real event, no more heartbeats — the fast items
    # pass through without timing out.
    first_real_index = next(
        i for i, r in enumerate(result) if not isinstance(r, _HeartbeatSentinel)
    )
    assert result[first_real_index] == "first"
    # Everything after the first real event must be a real event
    tail = result[first_real_index + 1 :]
    assert all(not isinstance(r, _HeartbeatSentinel) for r in tail)
    assert [r for r in tail] == ["b", "c", "d"]


@pytest.mark.asyncio
async def test_heartbeat_elapsed_ms_is_realistic():
    """The reported ``elapsed_ms`` should grow with the silence duration —
    a sanity check that the wrapper isn't reporting zero or stale values."""

    async def _silent_then():
        await asyncio.sleep(0.2)
        yield "done"

    result: list = []
    async for ev in _stream_with_heartbeats(_silent_then(), interval_seconds=0.06):
        result.append(ev)

    first_heartbeat = next(r for r in result if isinstance(r, _HeartbeatSentinel))
    # first interval is 60ms — heartbeat fires shortly after that
    assert first_heartbeat.elapsed_ms >= 40, (
        f"first heartbeat elapsed_ms {first_heartbeat.elapsed_ms} too small"
    )


@pytest.mark.asyncio
async def test_cancellation_closes_underlying_iterator():
    """When the SSE client disconnects mid-stream, the wrapper's ``finally``
    block must cancel the pending fetch task and call ``aclose()`` on the
    underlying iterator so the agent run cleanly stops instead of leaking.
    """
    closed = asyncio.Event()
    cancelled_or_returned = asyncio.Event()

    class _TrackedIter:
        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                cancelled_or_returned.set()
                raise
            raise StopAsyncIteration

        async def aclose(self):
            closed.set()

    async def _consume_with_cancel():
        async for ev in _stream_with_heartbeats(_TrackedIter(), interval_seconds=0.05):
            del ev  # drain — we cancel from outside

    task = asyncio.create_task(_consume_with_cancel())
    await asyncio.sleep(0.15)  # let the wrapper start fetching
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # The finally block in _stream_with_heartbeats must have run:
    # (a) cancelled the pending __anext__ task
    # (b) called aclose() on the iterator
    assert cancelled_or_returned.is_set(), "underlying __anext__ task not cancelled"
    assert closed.is_set(), "aclose() not called on the underlying iterator"
