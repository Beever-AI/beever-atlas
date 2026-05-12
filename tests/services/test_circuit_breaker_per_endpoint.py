"""PR-H.2: per-Endpoint circuit breaker registry — independent state per Endpoint."""

from __future__ import annotations

import pytest

from beever_atlas.services.circuit_breaker import (
    CircuitBreaker,
    get_breaker_for_endpoint,
    reset_circuit_breaker_for_tests,
    reset_endpoint_breakers_for_tests,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_circuit_breaker_for_tests()
    reset_endpoint_breakers_for_tests()


def test_lazily_creates_one_breaker_per_endpoint() -> None:
    b1 = get_breaker_for_endpoint("ep-1")
    b2 = get_breaker_for_endpoint("ep-2")
    assert isinstance(b1, CircuitBreaker)
    assert isinstance(b2, CircuitBreaker)
    assert b1 is not b2
    # Same id → same instance (memoised).
    assert get_breaker_for_endpoint("ep-1") is b1


@pytest.mark.asyncio
async def test_failures_on_one_endpoint_do_not_trip_another() -> None:
    b1 = get_breaker_for_endpoint("ep-1")
    b2 = get_breaker_for_endpoint("ep-2")
    # Trip ep-1 by recording threshold failures.
    threshold = b1._threshold
    for _ in range(threshold):
        await b1.record_failure(RuntimeError("boom"))
    assert b1.is_open() is True
    # ep-2 is untouched.
    assert b2.is_open() is False


@pytest.mark.asyncio
async def test_success_recording_resets_failure_count() -> None:
    b = get_breaker_for_endpoint("ep-1")
    await b.record_failure(RuntimeError("x"))
    await b.record_failure(RuntimeError("x"))
    await b.record_success()
    # Below threshold again — one more failure shouldn't trip.
    await b.record_failure(RuntimeError("x"))
    assert b.is_open() is False


@pytest.mark.asyncio
async def test_dispatch_records_failure_against_endpoint_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-429 exception from litellm.acompletion trips the endpoint's breaker
    after ``threshold`` failures."""
    import litellm

    from beever_atlas.services.llm_dispatch import dispatch_completion
    from beever_atlas.services.llm_throttle import reset_llm_throttle_for_tests

    reset_llm_throttle_for_tests()

    breaker = get_breaker_for_endpoint("ep-x")
    threshold = breaker._threshold

    async def boom(**_kw):
        raise RuntimeError("upstream 500")  # not a 429

    monkeypatch.setattr(litellm, "acompletion", boom)

    for _ in range(threshold):
        with pytest.raises(RuntimeError):
            await dispatch_completion(
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                messages=[{"role": "user", "content": "x"}],
                endpoint_id="ep-x",
            )

    assert breaker.is_open() is True
    # A different endpoint is unaffected.
    assert get_breaker_for_endpoint("ep-other").is_open() is False


@pytest.mark.asyncio
async def test_dispatch_records_success_against_endpoint_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean completion records a success — closes a half-open breaker."""
    import litellm

    from beever_atlas.services.llm_dispatch import dispatch_completion
    from beever_atlas.services.llm_throttle import reset_llm_throttle_for_tests
    from unittest.mock import MagicMock

    reset_llm_throttle_for_tests()

    breaker = get_breaker_for_endpoint("ep-y")
    # Manually set a couple of failures, then a successful dispatch should reset.
    await breaker.record_failure(RuntimeError("x"))
    await breaker.record_failure(RuntimeError("x"))

    async def ok(**_kw):
        resp = MagicMock()
        resp.status_code = 200
        choice = MagicMock()
        choice.message.content = "ok"
        resp.choices = [choice]
        return resp

    monkeypatch.setattr(litellm, "acompletion", ok)

    await dispatch_completion(
        provider="openai",
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        endpoint_id="ep-y",
    )
    # Failure counter reset; one more failure shouldn't trip.
    await breaker.record_failure(RuntimeError("x"))
    assert breaker.is_open() is False


@pytest.mark.asyncio
async def test_dispatch_without_endpoint_id_does_not_touch_endpoint_breakers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-A's bare-string call sites (no endpoint_id) don't create endpoint breakers."""
    import litellm

    from beever_atlas.services.llm_dispatch import dispatch_completion
    from beever_atlas.services.llm_throttle import reset_llm_throttle_for_tests
    from unittest.mock import MagicMock

    reset_llm_throttle_for_tests()

    async def ok(**_kw):
        resp = MagicMock()
        resp.status_code = 200
        choice = MagicMock()
        choice.message.content = "ok"
        resp.choices = [choice]
        return resp

    monkeypatch.setattr(litellm, "acompletion", ok)

    await dispatch_completion(
        provider="gemini",
        model="gemini/gemini-2.5-flash",
        messages=[{"role": "user", "content": "x"}],
        # no endpoint_id
    )
    # The registry stays empty — no spurious breaker created.
    from beever_atlas.services import circuit_breaker as cb_mod

    assert cb_mod._endpoint_breakers == {}


def test_reset_helper_clears_registry() -> None:
    get_breaker_for_endpoint("a")
    get_breaker_for_endpoint("b")
    from beever_atlas.services import circuit_breaker as cb_mod

    assert len(cb_mod._endpoint_breakers) == 2
    reset_endpoint_breakers_for_tests()
    assert cb_mod._endpoint_breakers == {}
