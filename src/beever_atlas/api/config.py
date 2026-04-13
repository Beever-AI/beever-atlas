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
