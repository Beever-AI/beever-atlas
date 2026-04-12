"""Platform connection model for self-service integrations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


class PlatformConnection(BaseModel):
    """Persisted record of a connected chat platform."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    platform: Literal["slack", "discord", "teams", "telegram", "file"]
    display_name: str
    encrypted_credentials: bytes
    credential_iv: bytes
    credential_tag: bytes
    selected_channels: list[str] = Field(default_factory=list)
    status: Literal["connected", "disconnected", "error"] = "connected"
    error_message: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    source: Literal["ui", "env"] = "ui"
