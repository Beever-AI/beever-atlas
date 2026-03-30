"""Mock adapter that reads from JSON fixture files for testing and local dev."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from beever_atlas.adapters.base import (
    BaseAdapter,
    ChannelInfo,
    NormalizedMessage,
)

logger = logging.getLogger(__name__)

_DEFAULT_FIXTURES_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures"


class MockAdapter(BaseAdapter):
    """Adapter that serves data from JSON fixture files.

    Activated by setting ADAPTER_MOCK=true. Useful for testing, local dev,
    and CI/CD without requiring platform credentials.
    """

    def __init__(self, fixtures_path: str | Path | None = None) -> None:
        self._fixtures_path = Path(fixtures_path) if fixtures_path else _DEFAULT_FIXTURES_PATH
        self._data: dict[str, Any] = {}
        self._load_fixtures()

    def _load_fixtures(self) -> None:
        conversations_file = self._fixtures_path / "slack_conversations.json"
        if conversations_file.exists():
            with open(conversations_file) as f:
                self._data = json.load(f)
            logger.info("MockAdapter loaded fixtures from %s", conversations_file)
        else:
            logger.warning("MockAdapter fixtures not found at %s", conversations_file)
            self._data = {"channels": [], "messages": {}, "users": {}}

    def normalize_message(self, raw: dict[str, Any]) -> NormalizedMessage:
        """Convert a fixture message dict to NormalizedMessage."""
        ts_str = raw.get("timestamp", raw.get("ts", "2024-01-01T00:00:00Z"))
        if isinstance(ts_str, str) and "T" in ts_str:
            timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            timestamp = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)

        return NormalizedMessage(
            content=raw.get("content", raw.get("text", "")),
            author=raw.get("author", raw.get("user", "unknown")),
            platform=raw.get("platform", "slack"),
            channel_id=raw.get("channel_id", ""),
            channel_name=raw.get("channel_name", ""),
            message_id=raw.get("message_id", raw.get("ts", "")),
            timestamp=timestamp,
            thread_id=raw.get("thread_id"),
            attachments=raw.get("attachments", []),
            reactions=raw.get("reactions", []),
            reply_count=raw.get("reply_count", 0),
            raw_metadata=raw,
        )

    async def fetch_history(
        self,
        channel_id: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[NormalizedMessage]:
        """Return fixture messages for the given channel."""
        raw_messages = self._data.get("messages", {}).get(channel_id, [])
        messages = [self.normalize_message(m) for m in raw_messages]

        if since:
            messages = [m for m in messages if m.timestamp >= since]

        messages.sort(key=lambda m: m.timestamp)
        return messages[:limit]

    async def fetch_thread(
        self,
        channel_id: str,
        thread_id: str,
    ) -> list[NormalizedMessage]:
        """Return fixture messages belonging to a specific thread."""
        raw_messages = self._data.get("messages", {}).get(channel_id, [])
        thread_msgs = []
        for m in raw_messages:
            mid = m.get("message_id", m.get("ts", ""))
            tid = m.get("thread_id")
            if mid == thread_id or tid == thread_id:
                thread_msgs.append(self.normalize_message(m))

        thread_msgs.sort(key=lambda m: m.timestamp)
        return thread_msgs

    async def get_channel_info(self, channel_id: str) -> ChannelInfo:
        """Return fixture channel info."""
        for ch in self._data.get("channels", []):
            if ch["channel_id"] == channel_id:
                return ChannelInfo(
                    channel_id=ch["channel_id"],
                    name=ch["name"],
                    platform=ch.get("platform", "slack"),
                    is_member=ch.get("is_member", True),
                    member_count=ch.get("member_count"),
                    topic=ch.get("topic"),
                    purpose=ch.get("purpose"),
                )
        raise KeyError(f"Channel {channel_id} not found in fixtures")

    async def list_channels(self) -> list[ChannelInfo]:
        """Return all fixture channels."""
        return [
            ChannelInfo(
                channel_id=ch["channel_id"],
                name=ch["name"],
                platform=ch.get("platform", "slack"),
                is_member=ch.get("is_member", True),
                member_count=ch.get("member_count"),
                topic=ch.get("topic"),
                purpose=ch.get("purpose"),
            )
            for ch in self._data.get("channels", [])
        ]
