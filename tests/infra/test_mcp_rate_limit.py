"""Tests for the principal-keyed MCP rate limiter (Phase 7, task 7.1).

Covers:
- ask_channel: 30/min per principal
- trigger_sync / refresh_wiki: 5/min per principal
- Everything else: 60/min per principal
- Principal isolation (A's calls don't affect B)
- Counter reset after 60s (monkeypatched clock)
- Cross-tool isolation (ask_channel counter separate from trigger_sync)
"""

from __future__ import annotations

import pytest

from beever_atlas.infra import mcp_rate_limit


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Ensure rate-limit state does not leak across test cases."""
    mcp_rate_limit.reset_state()
    # Restore real clock after each test.
    import time
    mcp_rate_limit._set_clock(time.monotonic)
    yield
    mcp_rate_limit.reset_state()
    mcp_rate_limit._set_clock(time.monotonic)


# ---------------------------------------------------------------------------
# ask_channel: 30/min
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ask_channel_allows_30_calls():
    """First 30 calls from principal A on ask_channel are allowed."""
    pid = "mcp:aaaa"
    for i in range(30):
        allowed, retry = await mcp_rate_limit.check_and_record(pid, "ask_channel")
        assert allowed is True, f"Call {i+1} should be allowed"
        assert retry is None


@pytest.mark.asyncio
async def test_ask_channel_31st_call_rate_limited():
    """31st call from the same principal on ask_channel is rate-limited."""
    pid = "mcp:aaaa"
    for _ in range(30):
        await mcp_rate_limit.check_and_record(pid, "ask_channel")

    allowed, retry = await mcp_rate_limit.check_and_record(pid, "ask_channel")
    assert allowed is False
    assert retry is not None
    assert retry >= 1


@pytest.mark.asyncio
async def test_principal_isolation_ask_channel():
    """Principal B's calls are unaffected when principal A hits ask_channel limit."""
    pid_a = "mcp:aaaa"
    pid_b = "mcp:bbbb"

    # Exhaust principal A's limit
    for _ in range(30):
        await mcp_rate_limit.check_and_record(pid_a, "ask_channel")
    allowed_a, _ = await mcp_rate_limit.check_and_record(pid_a, "ask_channel")
    assert allowed_a is False

    # Principal B should still be allowed
    for i in range(30):
        allowed_b, retry_b = await mcp_rate_limit.check_and_record(pid_b, "ask_channel")
        assert allowed_b is True, f"Principal B call {i+1} should be allowed"
        assert retry_b is None


@pytest.mark.asyncio
async def test_counter_resets_after_60s():
    """After 60 seconds the sliding window expires and calls are allowed again."""
    _now = [0.0]

    def fake_clock():
        return _now[0]

    mcp_rate_limit._set_clock(fake_clock)

    pid = "mcp:aaaa"
    # Exhaust limit at t=0
    for _ in range(30):
        await mcp_rate_limit.check_and_record(pid, "ask_channel")
    allowed, _ = await mcp_rate_limit.check_and_record(pid, "ask_channel")
    assert allowed is False

    # Advance clock past the window (61 seconds)
    _now[0] = 61.0

    # Now calls should be allowed again
    allowed_after, retry_after = await mcp_rate_limit.check_and_record(pid, "ask_channel")
    assert allowed_after is True
    assert retry_after is None


# ---------------------------------------------------------------------------
# trigger_sync: 5/min
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trigger_sync_allows_5_calls():
    """First 5 calls on trigger_sync are allowed."""
    pid = "mcp:cccc"
    for i in range(5):
        allowed, retry = await mcp_rate_limit.check_and_record(pid, "trigger_sync")
        assert allowed is True, f"Call {i+1} should be allowed"
        assert retry is None


@pytest.mark.asyncio
async def test_trigger_sync_6th_call_rate_limited():
    """6th call on trigger_sync is rate-limited."""
    pid = "mcp:cccc"
    for _ in range(5):
        await mcp_rate_limit.check_and_record(pid, "trigger_sync")
    allowed, retry = await mcp_rate_limit.check_and_record(pid, "trigger_sync")
    assert allowed is False
    assert retry is not None


@pytest.mark.asyncio
async def test_trigger_sync_counter_isolated_from_ask_channel():
    """trigger_sync and ask_channel have independent counters per principal."""
    pid = "mcp:dddd"
    # Exhaust trigger_sync limit
    for _ in range(5):
        await mcp_rate_limit.check_and_record(pid, "trigger_sync")
    ts_allowed, _ = await mcp_rate_limit.check_and_record(pid, "trigger_sync")
    assert ts_allowed is False

    # ask_channel counter for the same principal should be untouched
    ac_allowed, ac_retry = await mcp_rate_limit.check_and_record(pid, "ask_channel")
    assert ac_allowed is True
    assert ac_retry is None


# ---------------------------------------------------------------------------
# Default limit: 60/min
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_limit_60_per_min():
    """Tools not in the override map get 60/min."""
    pid = "mcp:eeee"
    for i in range(60):
        allowed, _ = await mcp_rate_limit.check_and_record(pid, "whoami")
        assert allowed is True, f"Call {i+1} should be allowed"
    allowed_61, retry = await mcp_rate_limit.check_and_record(pid, "whoami")
    assert allowed_61 is False
    assert retry is not None


# ---------------------------------------------------------------------------
# refresh_wiki: same 5/min as trigger_sync
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_wiki_has_5rpm_limit():
    """refresh_wiki shares the 5/min limit with trigger_sync."""
    pid = "mcp:ffff"
    for _ in range(5):
        await mcp_rate_limit.check_and_record(pid, "refresh_wiki")
    allowed, retry = await mcp_rate_limit.check_and_record(pid, "refresh_wiki")
    assert allowed is False
    assert retry is not None


@pytest.mark.asyncio
async def test_retry_after_is_positive_integer():
    """retry_after_seconds is always a positive int when rate-limited."""
    pid = "mcp:gggg"
    for _ in range(5):
        await mcp_rate_limit.check_and_record(pid, "trigger_sync")
    allowed, retry = await mcp_rate_limit.check_and_record(pid, "trigger_sync")
    assert allowed is False
    assert isinstance(retry, int)
    assert retry >= 1
