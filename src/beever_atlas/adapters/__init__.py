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
    "get_adapter",
]


def get_adapter() -> BaseAdapter:
    """Factory that returns MockAdapter when ADAPTER_MOCK=true, else ChatBridgeAdapter.

    The ChatBridgeAdapter calls the bot service bridge API, which uses Chat SDK
    to communicate with all platforms. No platform-specific logic needed here.

    Returns:
        A BaseAdapter instance.
    """
    if os.environ.get("ADAPTER_MOCK", "").lower() in ("true", "1", "yes"):
        return MockAdapter()

    return ChatBridgeAdapter()
