"""resolve_channel_name caching: only SUCCESSFUL resolutions are cached, so a
transient store error / not-yet-synced channel can't poison the cache and
permanently disable name resolution (which would break the cross-channel guard
and channel-name rendering)."""

from __future__ import annotations

import pytest

from beever_atlas.agents.tools import channel_resolver
from beever_atlas.agents.tools.channel_resolver import resolve_channel_name


@pytest.fixture(autouse=True)
def _clear_cache():
    channel_resolver._channel_name_cache.clear()
    yield
    channel_resolver._channel_name_cache.clear()


class _Store:
    def __init__(self, behaviors):
        # behaviors: list of either a string (return) or Exception (raise)
        self._behaviors = list(behaviors)
        self.calls = 0

    async def get_channel_display_name(self, channel_id):
        self.calls += 1
        b = self._behaviors.pop(0) if self._behaviors else None
        if isinstance(b, Exception):
            raise b
        return b


def _patch_store(monkeypatch, store):
    class _Stores:
        mongodb = store

    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: _Stores(), raising=False)


async def test_transient_error_is_not_cached_and_retries(monkeypatch):
    store = _Store([RuntimeError("mongo blip"), "basketball"])
    _patch_store(monkeypatch, store)
    # First call: store errors → raw id, NOT cached.
    assert await resolve_channel_name("C1") == "C1"
    assert "C1" not in channel_resolver._channel_name_cache
    # Second call: store recovers → resolves correctly (no poisoning).
    assert await resolve_channel_name("C1") == "basketball"
    assert channel_resolver._channel_name_cache["C1"] == "basketball"


async def test_missing_name_is_not_cached_and_retries(monkeypatch):
    store = _Store([None, "basketball"])
    _patch_store(monkeypatch, store)
    assert await resolve_channel_name("C1") == "C1"  # not synced yet → raw id
    assert "C1" not in channel_resolver._channel_name_cache
    assert await resolve_channel_name("C1") == "basketball"  # resolves once synced


async def test_successful_resolution_is_cached(monkeypatch):
    store = _Store(["basketball"])
    _patch_store(monkeypatch, store)
    assert await resolve_channel_name("C1") == "basketball"
    assert await resolve_channel_name("C1") == "basketball"
    assert store.calls == 1  # second call served from cache, no store hit
