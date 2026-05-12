"""PR-A: agent-llm-provider-pluggable — Ollama health cache TTL.

The pre-cutover cache was a single ``bool | None`` that, once flipped to
``False`` (e.g. during a brief daemon outage), stuck forever. After the change
the cache is a ``(value, monotonic_ts)`` tuple with a 30s TTL plus a public
``invalidate_ollama_cache()`` method that the dispatch layer calls on
connect-error.

Tests use ``time.monotonic`` patching to avoid actual wall-clock waits.
"""

from __future__ import annotations

from unittest.mock import patch

from beever_atlas.infra.config import Settings
from beever_atlas.llm.provider import LLMProvider, _OLLAMA_TTL_SECONDS


class _ManualClock:
    """Monotonic-clock stub for TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _provider_with_ollama(enabled: bool = True) -> LLMProvider:
    s = Settings(ollama_enabled=enabled, ollama_api_base="http://localhost:11434")
    return LLMProvider(s)


def test_cache_returns_within_ttl_window(monkeypatch):
    """Probe is hit once; subsequent calls within 30s reuse the cached value."""
    clock = _ManualClock()
    monkeypatch.setattr("beever_atlas.llm.provider.time.monotonic", clock)

    probe_calls = {"count": 0}

    def fake_get(url, timeout):  # noqa: ANN001 — httpx.get signature
        probe_calls["count"] += 1

        class _R:
            status_code = 200

        return _R()

    with patch("httpx.get", side_effect=fake_get):
        provider = _provider_with_ollama()
        assert provider._check_ollama_cached() is True
        assert provider._check_ollama_cached() is True
        clock.advance(_OLLAMA_TTL_SECONDS - 1)  # still within window
        assert provider._check_ollama_cached() is True

    assert probe_calls["count"] == 1, "cache should hit /api/tags only once"


def test_cache_refreshes_after_ttl(monkeypatch):
    """Past TTL, the next call re-probes; new state replaces old."""
    clock = _ManualClock()
    monkeypatch.setattr("beever_atlas.llm.provider.time.monotonic", clock)

    states = [True, False]  # first probe reachable, second down

    def fake_get(url, timeout):  # noqa: ANN001
        class _R:
            status_code = 200 if states.pop(0) else 503

        return _R()

    with patch("httpx.get", side_effect=fake_get):
        provider = _provider_with_ollama()
        assert provider._check_ollama_cached() is True
        clock.advance(_OLLAMA_TTL_SECONDS + 1)
        assert provider._check_ollama_cached() is False  # re-probed, daemon down


def test_force_invalidation_re_probes_immediately(monkeypatch):
    """``invalidate_ollama_cache`` defeats the TTL — next call re-probes now."""
    clock = _ManualClock()
    monkeypatch.setattr("beever_atlas.llm.provider.time.monotonic", clock)

    states = [False, True]  # first probe shows down, second shows recovered

    def fake_get(url, timeout):  # noqa: ANN001
        class _R:
            status_code = 200 if states.pop(0) else 503

        return _R()

    with patch("httpx.get", side_effect=fake_get):
        provider = _provider_with_ollama()
        assert provider._check_ollama_cached() is False
        # Daemon restarted; dispatch saw a connect error and invalidated:
        provider.invalidate_ollama_cache()
        # No clock advance — invalidation re-probes regardless of TTL:
        assert provider._check_ollama_cached() is True


def test_ollama_disabled_caches_false_without_probing(monkeypatch):
    """When ``ollama_enabled=False`` we never touch the network."""
    clock = _ManualClock()
    monkeypatch.setattr("beever_atlas.llm.provider.time.monotonic", clock)

    def boom(url, timeout):  # noqa: ANN001
        raise AssertionError("should not probe when ollama_enabled=False")

    with patch("httpx.get", side_effect=boom):
        provider = _provider_with_ollama(enabled=False)
        assert provider._check_ollama_cached() is False
        # Second call within the window: still no probe.
        assert provider._check_ollama_cached() is False


def test_reload_resets_cache(monkeypatch):
    """``LLMProvider.reload`` must invalidate the Ollama cache so a settings
    change is observed on the next resolve."""
    clock = _ManualClock()
    monkeypatch.setattr("beever_atlas.llm.provider.time.monotonic", clock)

    states = [True, False]

    def fake_get(url, timeout):  # noqa: ANN001
        class _R:
            status_code = 200 if states.pop(0) else 503

        return _R()

    with patch("httpx.get", side_effect=fake_get):
        provider = _provider_with_ollama()
        assert provider._check_ollama_cached() is True
        provider.reload({})  # admin saved an empty override map
        # Cache was reset — next probe runs even though TTL hasn't elapsed.
        assert provider._check_ollama_cached() is False
