"""Metadata-only adapter for ``platform="file"`` channels.

File-sourced channels enter the system via the import endpoint
(``services.file_importer`` → ``POST /imports/commit`` → BatchProcessor),
not by polling a remote API. This adapter exists so the rest of the
codebase (which dispatches on ``platform`` strings) has a uniform
contract. Its fetch methods intentionally return empty — there is no
upstream to pull from.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from beever_atlas.adapters.base import (
    BaseAdapter,
    ChannelInfo,
    NormalizedMessage,
)

logger = logging.getLogger(__name__)

PLATFORM_NAME = "file"


class FileAdapter(BaseAdapter):
    """No-op adapter whose sole purpose is to advertise ``platform="file"``."""

    def __init__(self, connection_id: str | None = None) -> None:
        self._connection_id = connection_id

    async def fetch_history(
        self,
        channel_id: str,
        since: datetime | None = None,
        limit: int = 100,
        before: str | None = None,
        order: str = "desc",
    ) -> list[NormalizedMessage]:
        logger.debug("FileAdapter.fetch_history is a no-op for channel=%s", channel_id)
        return []

    async def fetch_thread(
        self,
        channel_id: str,
        thread_id: str,
    ) -> list[NormalizedMessage]:
        return []

    async def get_channel_info(self, channel_id: str) -> ChannelInfo:
        return ChannelInfo(
            channel_id=channel_id,
            name=channel_id,
            platform=PLATFORM_NAME,
            is_member=True,
            connection_id=self._connection_id,
        )

    async def list_channels(self) -> list[ChannelInfo]:
        return []

    def normalize_message(self, raw: dict[str, Any]) -> NormalizedMessage:
        # File imports are parsed by services.file_importer.parse_file, which
        # constructs NormalizedMessage directly. This method is only present
        # to satisfy the BaseAdapter contract.
        raise NotImplementedError(
            "FileAdapter does not normalize raw messages; use "
            "services.file_importer.parse_file instead."
        )
