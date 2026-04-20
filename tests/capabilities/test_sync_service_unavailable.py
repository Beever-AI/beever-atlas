"""Tests for Fix #10: capabilities.sync.trigger_sync fails closed when
get_stores() raises instead of silently skipping the cooldown check.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from beever_atlas.capabilities.errors import ServiceUnavailable
from beever_atlas.capabilities.sync import trigger_sync


@pytest.mark.asyncio
async def test_trigger_sync_raises_service_unavailable_when_stores_fail():
    with patch(
        "beever_atlas.capabilities.sync.assert_channel_access", new=AsyncMock()
    ), patch(
        "beever_atlas.capabilities.sync.get_stores",
        side_effect=RuntimeError("stores init failed"),
    ):
        with pytest.raises(ServiceUnavailable) as exc_info:
            await trigger_sync("mcp:caller_abc123", "ch-xyz")

    assert exc_info.value.service == "stores"


@pytest.mark.asyncio
async def test_service_unavailable_preserves_cause():
    cause = RuntimeError("stores init failed")
    with patch(
        "beever_atlas.capabilities.sync.assert_channel_access", new=AsyncMock()
    ), patch(
        "beever_atlas.capabilities.sync.get_stores", side_effect=cause
    ):
        with pytest.raises(ServiceUnavailable) as exc_info:
            await trigger_sync("mcp:caller_abc123", "ch-xyz")

    assert exc_info.value.__cause__ is cause
