"""Unit tests for `_resolve_workspace_domain` in `api/ask.py`.

Covers: process-cache hit (adapter resolved once), and the never-raise
contract (any error path caches and returns None).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from beever_atlas.adapters.base import ChannelInfo
from beever_atlas.api import ask as ask_mod


@pytest.fixture(autouse=True)
def _clear_ws_cache():
    """Ensure a clean process cache per test."""
    ask_mod._WORKSPACE_DOMAIN_CACHE.clear()
    yield
    ask_mod._WORKSPACE_DOMAIN_CACHE.clear()


@pytest.mark.asyncio
async def test_resolve_caches_and_calls_adapter_once():
    """Second call for the same channel hits the cache; adapter resolved once."""
    info = ChannelInfo(
        channel_id="C1",
        name="eng",
        platform="slack",
        workspace_domain="beever",
    )
    adapter = AsyncMock()
    adapter.get_channel_info = AsyncMock(return_value=info)
    adapter.close = AsyncMock(return_value=None)

    resolve_adapter = AsyncMock(return_value=adapter)

    with patch(
        "beever_atlas.api.channels._resolve_adapter_for_channel",
        resolve_adapter,
    ):
        first = await ask_mod._resolve_workspace_domain("C1")
        second = await ask_mod._resolve_workspace_domain("C1")

    assert first == "beever"
    assert second == "beever"
    # Adapter resolution (and thus get_channel_info) happens only once.
    assert resolve_adapter.await_count == 1
    assert adapter.get_channel_info.await_count == 1
    assert ask_mod._WORKSPACE_DOMAIN_CACHE["C1"] == "beever"


@pytest.mark.asyncio
async def test_resolve_returns_none_on_exception_without_raising():
    """An error during resolution caches None and never raises."""
    resolve_adapter = AsyncMock(side_effect=RuntimeError("boom"))

    with patch(
        "beever_atlas.api.channels._resolve_adapter_for_channel",
        resolve_adapter,
    ):
        result = await ask_mod._resolve_workspace_domain("C2")

    assert result is None
    assert ask_mod._WORKSPACE_DOMAIN_CACHE["C2"] is None


@pytest.mark.asyncio
async def test_resolve_returns_none_for_non_slack_channel():
    """A channel whose adapter reports no workspace_domain caches/returns None."""
    info = ChannelInfo(channel_id="D1", name="general", platform="discord")
    adapter = AsyncMock()
    adapter.get_channel_info = AsyncMock(return_value=info)
    adapter.close = AsyncMock(return_value=None)

    resolve_adapter = AsyncMock(return_value=adapter)

    with patch(
        "beever_atlas.api.channels._resolve_adapter_for_channel",
        resolve_adapter,
    ):
        result = await ask_mod._resolve_workspace_domain("D1")

    assert result is None
    assert ask_mod._WORKSPACE_DOMAIN_CACHE["D1"] is None
