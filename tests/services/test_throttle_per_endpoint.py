"""PR-B.2: per-Endpoint throttle bucket keying.

The throttle keys on ``f"{provider}:{endpoint_id}"`` when an endpoint id is
supplied — so two same-provider Endpoints (e.g. OpenAI prod + OpenAI staging)
get independent rate-limit state. See design D7.
"""

from __future__ import annotations

import pytest

from beever_atlas.services.llm_throttle import (
    LLMThrottle,
    _make_bucket_key,
    reset_llm_throttle_for_tests,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_llm_throttle_for_tests()


def test_make_bucket_key_provider_only() -> None:
    assert _make_bucket_key("openai", None) == "openai"
    assert _make_bucket_key("OpenAI", None) == "openai"
    assert _make_bucket_key(" gemini ", None) == "gemini"


def test_make_bucket_key_with_endpoint() -> None:
    assert _make_bucket_key("openai", "ep-prod-123") == "openai:ep-prod-123"
    assert _make_bucket_key("openai", "ep-staging-456") == "openai:ep-staging-456"


def test_make_bucket_key_handles_blank_provider() -> None:
    assert _make_bucket_key("", None) == "unknown"


@pytest.mark.asyncio
async def test_two_endpoints_same_provider_use_distinct_buckets() -> None:
    """Acquire against two endpoint_ids creates two distinct buckets in the throttle."""
    throttle = LLMThrottle()

    async with throttle.acquire("openai", 100, endpoint_id="ep-prod"):
        pass
    async with throttle.acquire("openai", 100, endpoint_id="ep-staging"):
        pass

    keys = set(throttle._buckets.keys())
    assert "openai:ep-prod" in keys
    assert "openai:ep-staging" in keys
    # And the bare-provider key (no endpoint_id) is independent again.
    async with throttle.acquire("openai", 100):
        pass
    assert "openai" in throttle._buckets


@pytest.mark.asyncio
async def test_429_on_one_endpoint_does_not_affect_other() -> None:
    """A 429 reported against ep-prod sets cooldown on its bucket only."""
    throttle = LLMThrottle()

    async with throttle.acquire("openai", 100, endpoint_id="ep-prod"):
        pass
    async with throttle.acquire("openai", 100, endpoint_id="ep-staging"):
        pass

    throttle.report_429("openai", endpoint_id="ep-prod")

    prod_bucket = throttle._buckets["openai:ep-prod"]
    staging_bucket = throttle._buckets["openai:ep-staging"]
    # Cooldown set on prod bucket only.
    assert prod_bucket._cooldown_until > 0
    assert staging_bucket._cooldown_until == 0


@pytest.mark.asyncio
async def test_backward_compat_no_endpoint_id() -> None:
    """PR-A callers that pass only ``(provider, est_tokens)`` keep working."""
    throttle = LLMThrottle()

    async with throttle.acquire("anthropic", 100):
        pass
    assert "anthropic" in throttle._buckets
    # No spurious endpoint-suffixed key created.
    assert not any(k.startswith("anthropic:") for k in throttle._buckets)
