"""Unit tests for the injectable LLM CircuitBreaker (PR-C).

Replaces the module-level globals at services/batch_processor.py:53-54
that previously caused test-pollution under certain pytest orderings.
The new ``CircuitBreaker`` class is constructor-injected so each test
gets a fresh instance — no module-globals to bleed across tests.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/llm-circuit-breaker/``
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from beever_atlas.services.circuit_breaker import (
    CircuitBreaker,
    get_circuit_breaker,
    init_circuit_breaker,
    reset_circuit_breaker_for_tests,
)


# ---------------------------------------------------------------------------
# State transitions — closed → open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaker_starts_closed_and_allows() -> None:
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=60)
    assert breaker.state() == "closed"
    assert await breaker.allow() is True


@pytest.mark.asyncio
async def test_breaker_trips_to_open_at_threshold() -> None:
    """Spec scenario: ``Breaker trips after threshold consecutive failures``."""
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=60)
    for _ in range(2):
        await breaker.record_failure(RuntimeError("503"))
        assert breaker.state() == "closed"
    await breaker.record_failure(RuntimeError("503"))
    assert breaker.state() == "open"


@pytest.mark.asyncio
async def test_open_breaker_denies_calls() -> None:
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=60)
    await breaker.record_failure()
    await breaker.record_failure()
    assert breaker.state() == "open"
    assert await breaker.allow() is False


@pytest.mark.asyncio
async def test_success_in_closed_resets_failure_count() -> None:
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=60)
    await breaker.record_failure()
    await breaker.record_failure()
    await breaker.record_success()
    snapshot = breaker.snapshot()
    assert snapshot.consecutive_failures == 0
    # Now we can absorb another (threshold) failures before tripping.
    for _ in range(2):
        await breaker.record_failure()
    assert breaker.state() == "closed"


# ---------------------------------------------------------------------------
# Cooldown → half_open recovery path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_to_half_open_after_cooldown() -> None:
    """Spec scenario: ``Breaker enters half-open and recovers``."""
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    await breaker.record_failure()
    assert breaker.state() == "open"
    # Fast-forward time by patching time.monotonic.
    base = time.monotonic()
    with patch("beever_atlas.services.circuit_breaker.time.monotonic", lambda: base + 61):
        # First allow() after cooldown probes by transitioning to half_open.
        assert await breaker.allow() is True
        assert breaker.state() == "half_open"


@pytest.mark.asyncio
async def test_half_open_probe_success_closes_breaker() -> None:
    """Spec scenario: ``half_open → closed on probe success``."""
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    await breaker.record_failure()
    base = time.monotonic()
    with patch("beever_atlas.services.circuit_breaker.time.monotonic", lambda: base + 61):
        await breaker.allow()  # transitions to half_open
    await breaker.record_success()
    assert breaker.state() == "closed"
    assert breaker.snapshot().consecutive_failures == 0


@pytest.mark.asyncio
async def test_half_open_probe_failure_reopens_breaker() -> None:
    """Spec scenario: ``half_open → open on probe failure``."""
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    await breaker.record_failure()
    base = time.monotonic()
    with patch("beever_atlas.services.circuit_breaker.time.monotonic", lambda: base + 61):
        await breaker.allow()  # half_open
    await breaker.record_failure()
    assert breaker.state() == "open"


@pytest.mark.asyncio
async def test_open_breaker_does_not_advance_before_cooldown() -> None:
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    await breaker.record_failure()
    # Don't advance time — allow() must still deny.
    assert await breaker.allow() is False
    assert breaker.state() == "open"


@pytest.mark.asyncio
async def test_half_open_only_allows_one_concurrent_probe() -> None:
    """Two callers race through allow() while breaker is in cooldown.

    The first probe transitions to half_open and proceeds. The second
    must NOT also probe — half_open's whole point is that one probe at
    a time settles the state. Without this guarantee, a flapping
    upstream gets two requests on every cooldown boundary.
    """
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    await breaker.record_failure()
    base = time.monotonic()
    with patch("beever_atlas.services.circuit_breaker.time.monotonic", lambda: base + 61):
        first = await breaker.allow()
        assert first is True
        assert breaker.state() == "half_open"
        # Second caller arriving during the half_open window must be denied.
        second = await breaker.allow()
        assert second is False


# ---------------------------------------------------------------------------
# Snapshot / observability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_carries_state_metadata() -> None:
    breaker = CircuitBreaker(threshold=4, cooldown_seconds=120, provider_label="gemini")
    await breaker.record_failure()
    snapshot = breaker.snapshot()
    assert snapshot.state == "closed"
    assert snapshot.consecutive_failures == 1
    assert snapshot.threshold == 4
    assert snapshot.cooldown_seconds == 120


@pytest.mark.asyncio
async def test_state_transition_is_logged_with_event_name() -> None:
    """Spec: ``Breaker state observable via structured logs``."""
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=60)
    with patch("beever_atlas.services.circuit_breaker.logger") as mock_logger:
        await breaker.record_failure()
        await breaker.record_failure()  # → open
    # Should have been logged once (closed→open).
    log_calls = mock_logger.info.call_args_list
    assert any(
        call.args[0] == "circuit_breaker_state_change"
        and call.kwargs.get("extra", {}).get("from_state") == "closed"
        and call.kwargs.get("extra", {}).get("to_state") == "open"
        for call in log_calls
    )


# ---------------------------------------------------------------------------
# is_open synchronous accessor (used by LLMProvider failover)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_open_does_not_advance_state() -> None:
    """``is_open`` is a read-only check; calling it must NOT promote
    the breaker out of ``open`` into ``half_open`` even after cooldown.

    The LLMProvider failover seam reads ``is_open`` to decide whether
    to return the fallback model. If is_open() were stateful, every
    model resolution would consume the cooldown probe budget.
    """
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    await breaker.record_failure()
    base = time.monotonic()
    with patch("beever_atlas.services.circuit_breaker.time.monotonic", lambda: base + 9999):
        # Even years past the cooldown, is_open is read-only.
        assert breaker.is_open() is True
        assert breaker.state() == "open"


# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------


def test_get_circuit_breaker_returns_same_instance_within_process() -> None:
    reset_circuit_breaker_for_tests()
    a = get_circuit_breaker()
    b = get_circuit_breaker()
    assert a is b


def test_init_circuit_breaker_replaces_singleton() -> None:
    custom = CircuitBreaker(threshold=99, cooldown_seconds=99)
    init_circuit_breaker(custom)
    assert get_circuit_breaker() is custom
    reset_circuit_breaker_for_tests()


def test_reset_for_tests_drops_the_singleton() -> None:
    reset_circuit_breaker_for_tests()
    a = get_circuit_breaker()
    reset_circuit_breaker_for_tests()
    b = get_circuit_breaker()
    assert a is not b


# ---------------------------------------------------------------------------
# Manual reset (escape hatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_returns_to_closed_state() -> None:
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    await breaker.record_failure()
    assert breaker.state() == "open"
    breaker.reset()
    assert breaker.state() == "closed"
    assert breaker.snapshot().consecutive_failures == 0
