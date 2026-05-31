"""Platform connection model for self-service integrations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


class PlatformConnection(BaseModel):
    """Persisted record of a connected chat platform."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    platform: Literal["slack", "discord", "teams", "telegram", "mattermost", "file"]
    display_name: str
    encrypted_credentials: bytes
    credential_iv: bytes
    credential_tag: bytes
    selected_channels: list[str] = Field(default_factory=list)
    # Microsoft Graph team-ids (AAD group GUIDs) observed for this Teams
    # connection. The bot has no app-only Graph endpoint to enumerate
    # "teams this app is installed in", so identity is discovered from
    # Bot Framework webhooks and persisted here for parity with the
    # token-based bootstrap of Slack/Discord/Mattermost. Empty for every
    # non-Teams platform. Pydantic default — no DB migration required;
    # rows pre-dating this field decode as `[]`.
    teams_known_team_ids: list[str] = Field(default_factory=list)
    status: Literal["connected", "disconnected", "error"] = "connected"
    error_message: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    source: Literal["ui", "env"] = "ui"
    # Principal id (see `infra/auth.Principal.id`) of the user who created this
    # connection. `None` on documents written before RES-177; the platform
    # store's startup backfill rewrites `None` to the shared sentinel
    # ``"legacy:shared"`` so multi-tenant deployments have a single target
    # for explicit ownership assignment.
    owner_principal_id: str | None = None
