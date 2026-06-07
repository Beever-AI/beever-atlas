"""Application configuration API endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from beever_atlas.infra.config import get_settings

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/languages")
async def get_languages() -> dict:
    """Return supported languages and the default target language."""
    settings = get_settings()
    return {
        "supported_languages": settings.supported_languages_list,
        "default_target_language": settings.default_target_language,
    }


@router.get("/connectivity")
async def get_connectivity() -> dict:
    """Return the public bot URL and the exact inbound-webhook URLs to paste
    into Slack (Events API Request URL) and Teams (messaging endpoint).

    `public_bot_url` is empty when unset — the UI then shows guidance to
    configure a tunnel (local dev) or a public domain (production). Outbound
    transports (Discord, Mattermost, Slack Socket Mode) do not need this.
    """
    base = get_settings().public_bot_base
    return {
        "public_bot_url": base,
        "configured": bool(base),
        "webhooks": {
            # Legacy per-platform routes; the bot also accepts the
            # per-connection route /api/webhooks/{connectionId}.
            "slack": f"{base}/api/slack" if base else "",
            "teams": f"{base}/api/teams" if base else "",
        },
    }
