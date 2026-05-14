"""Unit tests for ``AutoOverviewSubscriber``
(sync-pipeline-feedback-and-auto-wiki §3.8 / §3.9).

Covers all 5 gates plus the in-flight idempotency contract. The
subscriber's hot path is mocked at the boundary methods
(``_get_stores`` / ``_generate_overview``) so these tests do not
require a real MongoDB / Weaviate / LLM.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from beever_atlas.services.auto_overview_subscriber import (
    AutoOverviewSubscriber,
)


# ---------------------------------------------------------------------------
# Fake stores helper — used by every test that exercises the gate logic.
# ---------------------------------------------------------------------------


class _FakeMongo:
    """Stub implementing the two surfaces the subscriber touches."""

    def __init__(
        self,
        *,
        counts: dict[str, int],
        existing_overview: bool,
    ) -> None:
        self._counts = counts
        self._existing_overview = existing_overview

    async def count_channel_messages_by_status(self, channel_id: str) -> dict[str, int]:
        return dict(self._counts)

    @property
    def db(self) -> Any:
        existing = self._existing_overview

        class _WikiPagesCollection:
            async def find_one(_self, query: dict, projection=None):  # noqa: ANN001
                return {"_id": "fake"} if existing else None

        class _DB:
            def __getitem__(_self, name: str):  # noqa: ANN001
                if name == "wiki_pages":
                    return _WikiPagesCollection()
                raise KeyError(name)

        return _DB()


class _FakeStores:
    def __init__(self, mongodb: _FakeMongo) -> None:
        self.mongodb = mongodb


def _make_subscriber(
    *,
    counts: dict[str, int],
    existing_overview: bool,
    feature_enabled: bool = True,
    min_facts_threshold: int = 5,
    generator: AsyncMock | None = None,
    language_resolver: AsyncMock | None = None,
) -> tuple[AutoOverviewSubscriber, AsyncMock]:
    """Construct a subscriber with all boundary methods mocked.

    Returns ``(subscriber, generator_mock)`` so tests can assert on the
    call count without reaching for the subscriber's private attrs.
    """
    gen = generator or AsyncMock()
    lang = language_resolver or AsyncMock(return_value="en")

    sub = AutoOverviewSubscriber(
        min_facts_threshold=min_facts_threshold,
        feature_flag_resolver=AsyncMock(return_value=feature_enabled),
        language_resolver=lang,
        generator=gen,
    )

    fake_stores = _FakeStores(_FakeMongo(counts=counts, existing_overview=existing_overview))
    sub._get_stores = lambda: fake_stores  # type: ignore[method-assign]

    return sub, gen


# ---------------------------------------------------------------------------
# Gate-by-gate no-op coverage (Task 3.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_op_when_feature_disabled() -> None:
    """Gate 1: feature flag off → never reach generator."""
    sub, gen = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 100, "failed": 0},
        existing_overview=False,
        feature_enabled=False,
    )

    await sub.on_extraction_done("C123", ["f1", "f2"])

    gen.assert_not_called()


@pytest.mark.asyncio
async def test_no_op_when_extraction_incomplete() -> None:
    """Gate 3: pending+extracting > 0 → no-op even with plenty of done facts."""
    sub, gen = _make_subscriber(
        counts={"pending": 200, "extracting": 200, "done": 311, "failed": 0},
        existing_overview=False,
    )

    await sub.on_extraction_done("C123", ["f1", "f2"])

    gen.assert_not_called()


@pytest.mark.asyncio
async def test_no_op_when_extraction_only_extracting_in_flight() -> None:
    """Gate 3: extracting > 0 alone is still mid-sync → no-op."""
    sub, gen = _make_subscriber(
        counts={"pending": 0, "extracting": 5, "done": 50, "failed": 0},
        existing_overview=False,
    )

    await sub.on_extraction_done("C123", ["f1"])

    gen.assert_not_called()


@pytest.mark.asyncio
async def test_no_op_when_below_min_facts() -> None:
    """Gate 4: ``done`` below threshold → no-op (threshold 5, done 3)."""
    sub, gen = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 3, "failed": 0},
        existing_overview=False,
        min_facts_threshold=5,
    )

    await sub.on_extraction_done("C123", ["f1", "f2", "f3"])

    gen.assert_not_called()


@pytest.mark.asyncio
async def test_no_op_when_overview_exists() -> None:
    """Gate 5: existing overview row → no-op (the second-sync invariant)."""
    sub, gen = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 100, "failed": 0},
        existing_overview=True,
    )

    await sub.on_extraction_done("C123", ["f1", "f2"])

    gen.assert_not_called()


# ---------------------------------------------------------------------------
# Happy-path + idempotency (Task 3.9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fires_exactly_once_when_qualified() -> None:
    """All gates pass → generator runs exactly once with the resolved language."""
    sub, gen = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
        language_resolver=AsyncMock(return_value="zh-Hant"),
    )

    await sub.on_extraction_done("C123", ["f1", "f2"])

    gen.assert_awaited_once_with("C123", "zh-Hant")


@pytest.mark.asyncio
async def test_serial_qualifying_events_only_one_generate() -> None:
    """Two events 200ms apart, both qualifying → only one generate call.

    The first event creates the overview (mocked side-effect flips
    ``existing_overview`` to True so the second event hits gate 5).
    """
    state: dict[str, bool] = {"existing": False}
    counts = {"pending": 0, "extracting": 0, "done": 50, "failed": 0}

    sub = AutoOverviewSubscriber(
        min_facts_threshold=5,
        feature_flag_resolver=AsyncMock(return_value=True),
        language_resolver=AsyncMock(return_value="en"),
        generator=AsyncMock(side_effect=lambda *_: state.update(existing=True)),
    )

    class _LiveMongo(_FakeMongo):
        @property
        def db(self) -> Any:
            class _WikiPagesCollection:
                async def find_one(_self, query: dict, projection=None):  # noqa: ANN001
                    return {"_id": "fake"} if state["existing"] else None

            class _DB:
                def __getitem__(_self, name: str):  # noqa: ANN001
                    if name == "wiki_pages":
                        return _WikiPagesCollection()
                    raise KeyError(name)

            return _DB()

    fake_stores = _FakeStores(_LiveMongo(counts=counts, existing_overview=False))
    sub._get_stores = lambda: fake_stores  # type: ignore[method-assign]

    await sub.on_extraction_done("C123", ["f1"])
    await asyncio.sleep(0)
    await sub.on_extraction_done("C123", ["f2"])

    assert sub._generator.await_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_concurrent_events_only_one_generate() -> None:
    """5 concurrent ``on_extraction_done`` events for the same channel
    must result in exactly ONE generator invocation.

    The generator sleeps so all 5 events overlap inside the in-flight
    window (the smoking-gun scenario: idempotency must hold under
    burst events arriving within a few ms of each other).
    """
    call_count = 0

    async def slow_generator(channel_id: str, language: str) -> None:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)

    sub, _ = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
        generator=AsyncMock(side_effect=slow_generator),
    )

    await asyncio.gather(*(sub.on_extraction_done("C123", [f"f{i}"]) for i in range(5)))

    assert call_count == 1


@pytest.mark.asyncio
async def test_inflight_slot_released_on_success() -> None:
    """After a successful generation the channel must be evicted from
    the in-flight set so a follow-up event (e.g. for a different
    channel state where the gate-5 check has changed) can re-fire."""
    sub, gen = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
    )

    await sub.on_extraction_done("C123", ["f1"])

    assert "C123" not in sub._inflight
    gen.assert_awaited_once()


@pytest.mark.asyncio
async def test_inflight_slot_released_on_generator_failure() -> None:
    """A generator exception must NOT leak the in-flight slot — the
    subscriber must always release the channel so manual retries work."""

    async def failing_generator(channel_id: str, language: str) -> None:
        raise RuntimeError("LLM quota exhausted")

    sub, _ = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
        generator=AsyncMock(side_effect=failing_generator),
    )

    # The subscriber swallows generator errors (manual Generate is the
    # recovery path) so this should not raise.
    await sub.on_extraction_done("C123", ["f1"])

    assert "C123" not in sub._inflight


@pytest.mark.asyncio
async def test_attempted_cleared_on_exception() -> None:
    """A generator exception must ALSO clear ``_attempted`` so the API
    returns to a pending state and the UI Retry button can re-fire.

    Regression: prior to the timeout-fix this set was sticky-on-success-
    only, which trapped the loading screen until process restart when
    the generator raised."""

    async def failing_generator(channel_id: str, language: str) -> None:
        raise RuntimeError("LLM quota exhausted")

    sub, _ = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
        generator=AsyncMock(side_effect=failing_generator),
    )

    await sub.on_extraction_done("C123", ["f1"])

    assert "C123" not in sub._attempted
    assert sub.is_inflight("C123") is False


@pytest.mark.asyncio
async def test_attempted_cleared_on_timeout(monkeypatch) -> None:
    """A generator that runs past ``_GENERATION_TIMEOUT_SECONDS`` must
    log + clear ``_attempted`` so the UI Retry button works."""

    async def slow_generator(channel_id: str, language: str) -> None:
        await asyncio.sleep(5)

    sub, _ = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
        generator=AsyncMock(side_effect=slow_generator),
    )
    # Force a tiny timeout for the test so we don't actually wait 10 minutes.
    monkeypatch.setattr(sub, "_GENERATION_TIMEOUT_SECONDS", 0.05)

    await sub.on_extraction_done("C123", ["f1"])

    assert "C123" not in sub._inflight
    assert "C123" not in sub._attempted
    assert sub.is_inflight("C123") is False


@pytest.mark.asyncio
async def test_force_reset_clears_both_sets() -> None:
    """``force_reset`` must clear both ``_inflight`` and ``_attempted``
    so the regenerate endpoint can return the channel to a pending
    state for a clean re-trigger."""
    sub = AutoOverviewSubscriber()
    sub._inflight.add("C123")
    sub._attempted["C123"] = datetime.now(tz=UTC)

    assert sub.is_inflight("C123") is True

    sub.force_reset("C123")

    assert "C123" not in sub._inflight
    assert "C123" not in sub._attempted
    assert sub.is_inflight("C123") is False
    assert sub.attempted_started_at("C123") is None


@pytest.mark.asyncio
async def test_different_channels_each_fire_once() -> None:
    """In-flight tracking is per-channel; events for different channels
    must not block each other."""
    sub, gen = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
    )

    await asyncio.gather(
        sub.on_extraction_done("C1", ["f1"]),
        sub.on_extraction_done("C2", ["f2"]),
        sub.on_extraction_done("C3", ["f3"]),
    )

    assert gen.await_count == 3
    called_channels = {call.args[0] for call in gen.await_args_list}
    assert called_channels == {"C1", "C2", "C3"}


# ---------------------------------------------------------------------------
# Default resolvers — feature flag + language fall through to settings.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_feature_flag_reads_settings(monkeypatch) -> None:
    """When no explicit resolver is injected, the subscriber reads
    ``Settings.auto_overview_wiki`` on every event."""

    class _StubSettings:
        auto_overview_wiki = False

    monkeypatch.setattr(
        "beever_atlas.infra.config.get_settings",
        lambda: _StubSettings(),
    )

    sub = AutoOverviewSubscriber()

    assert await sub._feature_enabled() is False


@pytest.mark.asyncio
async def test_default_language_falls_through_to_settings(monkeypatch) -> None:
    """No per-channel default → settings.default_target_language → ``en``."""

    class _StubSettings:
        default_target_language = "ja"

    monkeypatch.setattr(
        "beever_atlas.infra.config.get_settings",
        lambda: _StubSettings(),
    )

    # Force the policy resolver path to fail so the chain falls
    # through to the settings branch deterministically.
    async def _explode(*_args, **_kw):
        raise RuntimeError("no policy")

    monkeypatch.setattr(
        "beever_atlas.services.policy_resolver.resolve_effective_policy",
        _explode,
    )

    sub = AutoOverviewSubscriber()

    assert await sub._resolve_language("C123") == "ja"


@pytest.mark.asyncio
async def test_default_language_final_fallback_is_en(monkeypatch) -> None:
    """When neither policy nor settings resolve, the subscriber returns ``en``."""

    async def _explode(*_args, **_kw):
        raise RuntimeError("no policy")

    monkeypatch.setattr(
        "beever_atlas.services.policy_resolver.resolve_effective_policy",
        _explode,
    )

    def _explode_settings():
        raise RuntimeError("no settings")

    monkeypatch.setattr(
        "beever_atlas.infra.config.get_settings",
        _explode_settings,
    )

    sub = AutoOverviewSubscriber()

    assert await sub._resolve_language("C123") == "en"


# ---------------------------------------------------------------------------
# Bounded retry on WikiNotReadyError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_wiki_not_ready_then_succeeds(monkeypatch) -> None:
    """WikiNotReadyError on the first attempt must be retried, not
    propagated. After the retry succeeds the in-flight slot must
    release (so a later event can re-fire if needed). ``_attempted``
    intentionally stays sticky on success — it's cleared elsewhere
    by ``_safe_overview_state``'s row-existence check, NOT by the
    finally block (only terminal_failure pops it)."""
    from beever_atlas.capabilities.errors import WikiNotReadyError

    call_count = 0

    async def flaky_generator(channel_id: str, language: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise WikiNotReadyError("Consolidation is still in progress")

    sub, _ = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
        generator=AsyncMock(side_effect=flaky_generator),
    )
    # Zero-sleep so the test does not actually wait 30s between attempts.
    monkeypatch.setattr(sub, "_NOT_READY_RETRY_DELAY_SECONDS", 0)

    await sub.on_extraction_done("C123", ["f1"])

    assert call_count == 2  # one raise + one success
    assert "C123" not in sub._inflight


@pytest.mark.asyncio
async def test_retries_exhausted_clears_attempted(monkeypatch) -> None:
    """If ``WikiNotReadyError`` is raised on every attempt the subscriber
    must give up after ``_NOT_READY_MAX_RETRIES`` retries and clear
    ``_attempted`` so the UI Retry button can re-fire."""
    from beever_atlas.capabilities.errors import WikiNotReadyError

    call_count = 0

    async def always_not_ready(channel_id: str, language: str) -> None:
        nonlocal call_count
        call_count += 1
        raise WikiNotReadyError("never settles")

    sub, _ = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
        generator=AsyncMock(side_effect=always_not_ready),
    )
    monkeypatch.setattr(sub, "_NOT_READY_RETRY_DELAY_SECONDS", 0)

    await sub.on_extraction_done("C123", ["f1"])

    # initial attempt + _NOT_READY_MAX_RETRIES retries
    assert call_count == sub._NOT_READY_MAX_RETRIES + 1
    assert "C123" not in sub._attempted
    assert "C123" not in sub._inflight


@pytest.mark.asyncio
async def test_non_not_ready_exception_is_not_retried() -> None:
    """Only WikiNotReadyError triggers retries. Other exceptions (LLM
    quota, network blip) must fail-fast on the first call so we don't
    rack up cost on a permanent failure."""
    call_count = 0

    async def quota_failure(channel_id: str, language: str) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("LLM quota exhausted")

    sub, _ = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
        generator=AsyncMock(side_effect=quota_failure),
    )

    await sub.on_extraction_done("C123", ["f1"])

    assert call_count == 1
    assert "C123" not in sub._attempted


@pytest.mark.asyncio
async def test_total_retry_budget_bounded_by_generation_timeout(monkeypatch) -> None:
    """The outer ``wait_for`` budget must cover ALL attempts + sleeps,
    not reset per attempt. Otherwise a slow upstream can stall the
    subscriber for tens of minutes (code-reviewer flagged this pre-merge).
    Force a tiny outer timeout and a generator that holds long enough
    to confirm the outer wait_for fires across the retry loop."""
    from beever_atlas.capabilities.errors import WikiNotReadyError

    async def slow_not_ready(channel_id: str, language: str) -> None:
        # Each attempt takes longer than the outer timeout / max_retries
        # so cumulative time exceeds the budget within the first retry.
        await asyncio.sleep(0.1)
        raise WikiNotReadyError("slow")

    sub, _ = _make_subscriber(
        counts={"pending": 0, "extracting": 0, "done": 50, "failed": 0},
        existing_overview=False,
        generator=AsyncMock(side_effect=slow_not_ready),
    )
    monkeypatch.setattr(sub, "_GENERATION_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(sub, "_NOT_READY_RETRY_DELAY_SECONDS", 0.05)

    start = asyncio.get_event_loop().time()
    await sub.on_extraction_done("C123", ["f1"])
    elapsed = asyncio.get_event_loop().time() - start

    # Outer wait_for fires well before the sum of all per-attempt budgets
    # (which would otherwise be ~ 3 retries * (0.1 + 0.05) + 0.1 = 0.55s).
    assert elapsed < 0.4, f"retry budget escaped outer wait_for ({elapsed:.2f}s)"
    assert "C123" not in sub._attempted
