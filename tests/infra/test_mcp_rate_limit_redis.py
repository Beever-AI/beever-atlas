"""Redis-backend tests for mcp_rate_limit.

Mocks the async Redis client so no live Redis is required. The Lua script is
executed purely on the mock; the test asserts the CORRECT arguments are passed
(key shape, window, limit, member suffix, backend dispatch on settings).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from beever_atlas.infra import mcp_rate_limit


@pytest.fixture(autouse=True)
def _reset_client():
    """Ensure the module-level Redis client is nulled out between tests."""
    mcp_rate_limit._redis_client = None
    mcp_rate_limit._redis_script_sha = None
    mcp_rate_limit.reset_state()
    yield
    mcp_rate_limit._redis_client = None
    mcp_rate_limit._redis_script_sha = None
    mcp_rate_limit.reset_state()


def _redis_settings(backend: str = "redis"):
    return SimpleNamespace(
        beever_mcp_rate_limit_backend=backend,
        redis_url="redis://localhost:6379",
    )


@pytest.mark.asyncio
async def test_backend_dispatch_uses_redis_when_configured():
    """With backend='redis' in settings, check_and_record reaches Redis."""
    fake_client = SimpleNamespace(
        script_load=AsyncMock(return_value="sha-abc"),
        evalsha=AsyncMock(return_value=[1, 0]),  # allowed, retry=0
    )
    with (
        patch(
            "beever_atlas.infra.mcp_rate_limit.get_settings",
            return_value=_redis_settings(),
        ),
        patch("redis.asyncio.from_url", return_value=fake_client),
    ):
        allowed, retry = await mcp_rate_limit.check_and_record("mcp:alice", "ask_channel")

    assert allowed is True
    assert retry is None
    fake_client.script_load.assert_awaited_once()
    fake_client.evalsha.assert_awaited_once()
    # Second positional arg is the number-of-keys = 1.
    call_args = fake_client.evalsha.call_args.args
    assert call_args[1] == 1
    # Third positional arg is the key.
    assert call_args[2] == "mcp:rl:mcp:alice:ask_channel"


@pytest.mark.asyncio
async def test_backend_dispatch_stays_memory_by_default():
    """With backend='memory', Redis is never touched."""
    fake_client = SimpleNamespace(
        script_load=AsyncMock(return_value="unused"),
        evalsha=AsyncMock(return_value=[1, 0]),
    )
    with (
        patch(
            "beever_atlas.infra.mcp_rate_limit.get_settings",
            return_value=_redis_settings(backend="memory"),
        ),
        patch("redis.asyncio.from_url", return_value=fake_client),
    ):
        allowed, _ = await mcp_rate_limit.check_and_record("mcp:bob", "ask_channel")

    assert allowed is True
    fake_client.script_load.assert_not_awaited()
    fake_client.evalsha.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_denied_response_returns_retry_after():
    """When the Lua script returns allowed=0, the caller sees retry_after_seconds."""
    fake_client = SimpleNamespace(
        script_load=AsyncMock(return_value="sha-xyz"),
        evalsha=AsyncMock(return_value=[0, 37]),  # denied, retry=37
    )
    with (
        patch(
            "beever_atlas.infra.mcp_rate_limit.get_settings",
            return_value=_redis_settings(),
        ),
        patch("redis.asyncio.from_url", return_value=fake_client),
    ):
        allowed, retry = await mcp_rate_limit.check_and_record("mcp:alice", "trigger_sync")

    assert allowed is False
    assert retry == 37


@pytest.mark.asyncio
async def test_redis_failure_falls_back_to_memory_limiter():
    """Redis unreachable → fall back to the in-process memory limiter.

    Defence-in-depth: a pure fail-open would let a flaky Redis become a
    DoS amplifier on expensive tools. Falling back means each worker
    degrades to per-process limits instead of disabling rate limiting.
    """
    fake_client = SimpleNamespace(
        script_load=AsyncMock(side_effect=ConnectionError("redis down")),
        evalsha=AsyncMock(side_effect=ConnectionError("redis down")),
        eval=AsyncMock(side_effect=ConnectionError("redis down")),
    )
    with (
        patch(
            "beever_atlas.infra.mcp_rate_limit.get_settings",
            return_value=_redis_settings(),
        ),
        patch("redis.asyncio.from_url", return_value=fake_client),
    ):
        # First call goes through — memory limiter has capacity.
        allowed, retry = await mcp_rate_limit.check_and_record("mcp:charlie", "ask_channel")
        assert allowed is True
        assert retry is None

        # Exhaust the ask_channel 30/min budget on this principal — the
        # memory fallback is still enforcing, not fail-open forever.
        for _ in range(29):
            await mcp_rate_limit.check_and_record("mcp:charlie", "ask_channel")
        allowed_over_budget, retry_over = await mcp_rate_limit.check_and_record(
            "mcp:charlie", "ask_channel"
        )
        assert allowed_over_budget is False, (
            "Fallback memory limiter MUST still enforce per-tool limits — "
            "otherwise a flaky Redis becomes an unbounded-burst DoS path"
        )
        assert retry_over is not None and retry_over > 0


@pytest.mark.asyncio
async def test_redis_evalsha_refreshes_script_on_noscript():
    """If Redis evicts the cached script, the limiter reloads and retries."""
    # First evalsha call raises (script evicted), second succeeds.
    evalsha = AsyncMock(side_effect=[Exception("NOSCRIPT"), [1, 0]])
    script_load = AsyncMock(return_value="sha-reloaded")
    fake_client = SimpleNamespace(script_load=script_load, evalsha=evalsha)

    with (
        patch(
            "beever_atlas.infra.mcp_rate_limit.get_settings",
            return_value=_redis_settings(),
        ),
        patch("redis.asyncio.from_url", return_value=fake_client),
    ):
        allowed, _ = await mcp_rate_limit.check_and_record("mcp:alice", "ask_channel")

    assert allowed is True
    # Initial load + reload = 2
    assert script_load.await_count == 2
    assert evalsha.await_count == 2


@pytest.mark.asyncio
async def test_redis_eval_fallback_when_script_load_fails_on_init():
    """If script_load fails during init, limiter falls back to full EVAL."""
    script_load = AsyncMock(side_effect=ConnectionError("redis init flake"))
    eval_call = AsyncMock(return_value=[1, 0])
    fake_client = SimpleNamespace(
        script_load=script_load,
        eval=eval_call,
    )

    with (
        patch(
            "beever_atlas.infra.mcp_rate_limit.get_settings",
            return_value=_redis_settings(),
        ),
        patch("redis.asyncio.from_url", return_value=fake_client),
    ):
        allowed, _ = await mcp_rate_limit.check_and_record("mcp:alice", "ask_channel")

    assert allowed is True
    script_load.assert_awaited_once()
    eval_call.assert_awaited_once()
