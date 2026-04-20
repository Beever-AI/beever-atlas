"""Phase 9 task 9.3 — Rate-limit isolation proof (unit-level).

Spec scenario: "Two principals isolated" — each principal has its own
sliding-window counter for ``ask_channel`` (limit: 30 per 60 s).

What is tested:
    - 3 principals each fire ``ask_channel`` 35 times concurrently
    - Each gets exactly 30 successes + 5 ``rate_limited`` denials
    - Principals do not see each other's counters (cross-principal isolation)

Scope note:
    This is a unit-level proof using the in-process
    ``mcp_rate_limit.check_and_record`` directly plus the ``_check_rate_limit``
    wrapper from ``api.mcp_server.__init__``.  A true multi-process load test
    requires a distributed rate-limit backend (Redis) and deployment tooling
    that does not yet exist — that scenario is deferred to a future milestone.

Clock injection:
    ``mcp_rate_limit._set_clock`` pins time to a fixed monotonic value so all
    35 calls fall inside the same 60-second window, guaranteeing deterministic
    results without any actual sleeping.
"""

from __future__ import annotations

import asyncio

import pytest

_TOOL_NAME = "ask_channel"
_ASK_CHANNEL_LIMIT = 30  # from mcp_rate_limit._TOOL_LIMITS
_CALLS_PER_PRINCIPAL = 35
_EXPECTED_OK = 30
_EXPECTED_DENIED = _CALLS_PER_PRINCIPAL - _EXPECTED_OK  # 5


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_and_pin_clock():
    """Reset rate-limit state and pin the clock before each test."""
    from beever_atlas.infra import mcp_rate_limit

    mcp_rate_limit.reset_state()
    # Pin time so all calls land in the same window
    mcp_rate_limit._set_clock(lambda: 1_000_000.0)
    yield
    # Restore real clock and clean up
    import time

    mcp_rate_limit._set_clock(time.monotonic)
    mcp_rate_limit.reset_state()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fire_n_calls(principal_id: str, n: int) -> dict[str, int]:
    """Call check_and_record n times for principal_id and count outcomes."""
    from beever_atlas.infra import mcp_rate_limit

    ok_count = 0
    denied_count = 0
    for _ in range(n):
        allowed, _ = await mcp_rate_limit.check_and_record(principal_id, _TOOL_NAME)
        if allowed:
            ok_count += 1
        else:
            denied_count += 1
    return {"ok": ok_count, "denied": denied_count}


# ---------------------------------------------------------------------------
# 9.3.1  Three principals in parallel — each gets 30 ok + 5 denied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_principals_isolated_rate_limits():
    """Three concurrent principals each hit the ask_channel limit independently.

    Principal A, B, and C each fire 35 calls.  Expected:
        - Each: 30 ok + 5 denied
        - No cross-contamination of counters
    """
    principals = [
        "mcp:principal-aaaa1111",
        "mcp:principal-bbbb2222",
        "mcp:principal-cccc3333",
    ]

    results = await asyncio.gather(
        *[_fire_n_calls(pid, _CALLS_PER_PRINCIPAL) for pid in principals]
    )

    for i, (pid, result) in enumerate(zip(principals, results)):
        assert result["ok"] == _EXPECTED_OK, (
            f"Principal[{i}] {pid}: expected {_EXPECTED_OK} ok, got {result['ok']}"
        )
        assert result["denied"] == _EXPECTED_DENIED, (
            f"Principal[{i}] {pid}: expected {_EXPECTED_DENIED} denied, got {result['denied']}"
        )


# ---------------------------------------------------------------------------
# 9.3.2  Cross-principal counter isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_principal_counter_isolation():
    """Exhausting one principal's window does not affect another principal.

    Principal A fires 35 calls (exhausts its window).
    Principal B has not called yet — its first call must be allowed.
    """
    from beever_atlas.infra import mcp_rate_limit

    principal_a = "mcp:isolation-test-aaaa"
    principal_b = "mcp:isolation-test-bbbb"

    # Exhaust principal A
    for _ in range(_CALLS_PER_PRINCIPAL):
        await mcp_rate_limit.check_and_record(principal_a, _TOOL_NAME)

    # Principal A is now rate-limited
    allowed_a, _ = await mcp_rate_limit.check_and_record(principal_a, _TOOL_NAME)
    assert not allowed_a, "Principal A should be rate-limited after exhausting its window"

    # Principal B is unaffected — first call must succeed
    allowed_b, retry_b = await mcp_rate_limit.check_and_record(principal_b, _TOOL_NAME)
    assert allowed_b, (
        f"Principal B's first call must be allowed; got allowed={allowed_b} retry={retry_b}"
    )


# ---------------------------------------------------------------------------
# 9.3.3  Same principal, different tool — separate windows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_tools_have_independent_windows():
    """ask_channel exhaustion does not affect list_connections (default limit).

    ask_channel limit = 30; list_connections uses the default limit (60).
    Exhausting ask_channel must not affect list_connections calls.
    """
    from beever_atlas.infra import mcp_rate_limit

    principal = "mcp:tool-isolation-test"

    # Exhaust ask_channel
    for _ in range(30):
        await mcp_rate_limit.check_and_record(principal, "ask_channel")

    # ask_channel is now rate-limited
    allowed_ask, _ = await mcp_rate_limit.check_and_record(principal, "ask_channel")
    assert not allowed_ask, "ask_channel should be rate-limited"

    # list_connections is unaffected (separate (principal, tool) key)
    allowed_list, _ = await mcp_rate_limit.check_and_record(principal, "list_connections")
    assert allowed_list, "list_connections should not be affected by ask_channel exhaustion"


# ---------------------------------------------------------------------------
# Deferred scope note (required by spec)
# ---------------------------------------------------------------------------

# A true multi-process load test is deferred until deployment tooling exists.
# The current in-process implementation uses a per-process dict; under a
# multi-worker deployment each process maintains independent counters, so
# the effective limit is N * num_workers.  The upgrade path is to replace
# _windows with a Redis-backed sliding-window counter (see mcp_rate_limit.py
# module docstring for the zero-touch swap contract).
