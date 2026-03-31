"""Multi-platform adapter layer for message ingestion."""

from __future__ import annotations

import os

from beever_atlas.adapters.base import (
    BaseAdapter,
    ChannelInfo,
    ConfigurationError,
    NormalizedMessage,
)
from beever_atlas.adapters.bridge import BridgeError, ChatBridgeAdapter
from beever_atlas.adapters.mock import MockAdapter

__all__ = [
    "BaseAdapter",
    "BridgeError",
    "ChannelInfo",
    "ChatBridgeAdapter",
    "ConfigurationError",
    "MockAdapter",
    "NormalizedMessage",
    "close_adapter",
    "get_adapter",
]

_adapter: BaseAdapter | None = None


def get_adapter() -> BaseAdapter:
    """Factory that returns MockAdapter when ADAPTER_MOCK=true, else ChatBridgeAdapter.

    The ChatBridgeAdapter calls the bot service bridge API, which uses Chat SDK
    to communicate with all platforms. No platform-specific logic needed here.

    Returns:
        A BaseAdapter instance.
    """
    global _adapter
    if _adapter is not None:
        return _adapter
    if os.environ.get("ADAPTER_MOCK", "").lower() in ("true", "1", "yes"):
        _adapter = MockAdapter()
    else:
        _adapter = ChatBridgeAdapter()
    return _adapter


async def close_adapter() -> None:
    """Close the singleton adapter (if any) and reset it."""
    global _adapter
    if _adapter is None:
        return
    await _adapter.close()
    _adapter = None
