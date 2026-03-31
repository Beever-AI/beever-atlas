"""Base adapter types and abstract class for multi-platform message ingestion."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class NormalizedMessage:
    """Platform-agnostic message representation."""

    content: str
    author: str
    platform: str  # "slack" | "teams" | "discord"
    channel_id: str
    channel_name: str
    message_id: str
    timestamp: datetime
    thread_id: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[dict[str, Any]] = field(default_factory=list)
    reply_count: int = 0
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    author_name: str = ""
    author_image: str = ""


@dataclass
class ChannelInfo:
    """Platform-agnostic channel metadata."""

    channel_id: str
    name: str
    platform: str
    is_member: bool = False
    member_count: int | None = None
    topic: str | None = None
    purpose: str | None = None


class ConfigurationError(Exception):
    """Raised when adapter configuration is invalid."""


class BaseAdapter(abc.ABC):
    """Abstract base class for platform adapters.

    Each platform adapter (Slack, Teams, Discord) implements this interface
    to provide a unified way to fetch messages and channel metadata.
    """

    @abc.abstractmethod
    async def fetch_history(
        self,
        channel_id: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[NormalizedMessage]:
        """Fetch message history for a channel."""

    @abc.abstractmethod
    async def fetch_thread(
        self,
        channel_id: str,
        thread_id: str,
    ) -> list[NormalizedMessage]:
        """Fetch all messages in a thread."""

    @abc.abstractmethod
    async def get_channel_info(self, channel_id: str) -> ChannelInfo:
        """Get metadata for a channel."""

    @abc.abstractmethod
    async def list_channels(self) -> list[ChannelInfo]:
        """List all accessible channels."""

    @abc.abstractmethod
    def normalize_message(self, raw: dict[str, Any]) -> NormalizedMessage:
        """Convert a platform-specific message dict to NormalizedMessage."""

    async def close(self) -> None:
        """Optional cleanup hook for adapters with network clients."""
        return None
