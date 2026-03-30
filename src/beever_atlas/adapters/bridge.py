"""Chat Bridge adapter — calls the bot service bridge API for all platform data.

The bot service (TypeScript + Chat SDK) is the single gateway to all chat platforms.
This adapter calls its /bridge/* REST endpoints via httpx, keeping the Python backend
completely platform-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from beever_atlas.adapters.base import (
    BaseAdapter,
    ChannelInfo,
    ConfigurationError,
    NormalizedMessage,
)

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1.0


class BridgeError(RuntimeError):
    """Raised when the bridge API returns an error."""

    def __init__(self, message: str, status_code: int = 0, code: str = "BRIDGE_ERROR"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class ChatBridgeAdapter(BaseAdapter):
    """Adapter that fetches data from the bot service bridge API.

    The bot service uses Chat SDK with platform adapters (Slack, Teams, Discord)
    and exposes their fetch capabilities via REST endpoints at /bridge/*.
    """

    def __init__(
        self,
        bridge_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._bridge_url = bridge_url or os.environ.get("BRIDGE_URL", "http://localhost:3001")
        self._api_key = api_key or os.environ.get("BRIDGE_API_KEY", "")

        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        self._client = httpx.AsyncClient(
            base_url=self._bridge_url,
            headers=headers,
            timeout=30.0,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Make an HTTP request to the bridge with retry on transient errors."""
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                response = await self._client.request(method, path, **kwargs)

                if response.status_code == 404:
                    data = response.json()
                    raise KeyError(data.get("error", f"Not found: {path}"))

                if response.status_code == 429 or response.status_code >= 500:
                    wait = _BASE_BACKOFF_SECONDS * (2**attempt)
                    logger.warning(
                        "Bridge returned %d on %s, retrying in %.1fs (attempt %d/%d)",
                        response.status_code,
                        path,
                        wait,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue

                if response.status_code >= 400:
                    data = response.json()
                    raise BridgeError(
                        data.get("error", f"Bridge error: {response.status_code}"),
                        status_code=response.status_code,
                        code=data.get("code", "BRIDGE_ERROR"),
                    )

                return response.json()

            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_exc = e
                wait = _BASE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "Bridge connection error on %s: %s, retrying in %.1fs (attempt %d/%d)",
                    path,
                    str(e),
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                await asyncio.sleep(wait)

        raise BridgeError(
            f"Bridge request failed after {_MAX_RETRIES} retries: {last_exc}",
            code="CONNECTION_ERROR",
        )

    def normalize_message(self, raw: dict[str, Any]) -> NormalizedMessage:
        """Convert bridge JSON to NormalizedMessage.

        The bridge already returns pre-normalized JSON, so this is a simple mapping.
        """
        ts_str = raw.get("timestamp", "")
        if ts_str:
            timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            timestamp = datetime.now(timezone.utc)

        return NormalizedMessage(
            content=raw.get("content", ""),
            author=raw.get("author", "unknown"),
            platform=raw.get("platform", "slack"),
            channel_id=raw.get("channel_id", ""),
            channel_name=raw.get("channel_name", ""),
            message_id=raw.get("message_id", ""),
            timestamp=timestamp,
            thread_id=raw.get("thread_id"),
            attachments=raw.get("attachments", []),
            reactions=raw.get("reactions", []),
            reply_count=raw.get("reply_count", 0),
            raw_metadata=raw,
            author_name=raw.get("author_name", ""),
            author_image=raw.get("author_image", ""),
        )

    async def fetch_history(
        self,
        channel_id: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[NormalizedMessage]:
        """Fetch channel message history via bridge."""
        params: dict[str, str] = {"limit": str(limit)}
        if since:
            params["since"] = since.isoformat()

        data = await self._request(
            "GET",
            f"/bridge/channels/{channel_id}/messages",
            params=params,
        )

        return [self.normalize_message(m) for m in data.get("messages", [])]

    async def fetch_thread(
        self,
        channel_id: str,
        thread_id: str,
    ) -> list[NormalizedMessage]:
        """Fetch thread messages via bridge."""
        data = await self._request(
            "GET",
            f"/bridge/channels/{channel_id}/threads/{thread_id}/messages",
        )

        return [self.normalize_message(m) for m in data.get("messages", [])]

    async def get_channel_info(self, channel_id: str) -> ChannelInfo:
        """Get channel metadata via bridge."""
        data = await self._request("GET", f"/bridge/channels/{channel_id}")

        return ChannelInfo(
            channel_id=data.get("channel_id", channel_id),
            name=data.get("name", ""),
            platform=data.get("platform", "slack"),
            is_member=data.get("is_member", False),
            member_count=data.get("member_count"),
            topic=data.get("topic"),
            purpose=data.get("purpose"),
        )

    async def list_channels(self) -> list[ChannelInfo]:
        """List all accessible channels via bridge."""
        data = await self._request("GET", "/bridge/channels")

        return [
            ChannelInfo(
                channel_id=ch.get("channel_id", ""),
                name=ch.get("name", ""),
                platform=ch.get("platform", "slack"),
                is_member=ch.get("is_member", False),
                member_count=ch.get("member_count"),
                topic=ch.get("topic"),
                purpose=ch.get("purpose"),
            )
            for ch in data.get("channels", [])
        ]
