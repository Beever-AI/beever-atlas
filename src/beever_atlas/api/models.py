"""API endpoints for per-agent model configuration."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from beever_atlas.api._deprecation import deprecated_route
from pydantic import BaseModel

from beever_atlas.llm.model_resolver import (
    AGENT_NAMES,
    DEFAULT_AGENT_MODELS,
    KNOWN_GEMINI_MODELS,
    KNOWN_OLLAMA_MODELS,
    MODEL_PRESETS,
    validate_model_string,
)
from beever_atlas.llm.ollama import check_ollama_health
from beever_atlas.llm.provider import get_llm_provider
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/settings/models",
    tags=["models"],
    dependencies=[Depends(deprecated_route("/api/settings/assignments"))],
)


# ── Request / Response Models ────────────────────────────────────────────


class AgentModelConfigResponse(BaseModel):
    models: dict[str, str]
    defaults: dict[str, str]
    updated_at: str | None = None


class UpdateModelsRequest(BaseModel):
    models: dict[str, str]


class AvailableModelsResponse(BaseModel):
    gemini: list[str]
    ollama: list[str]
    ollama_connected: bool


class PresetRequest(BaseModel):
    preset: str


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("", response_model=AgentModelConfigResponse)
async def get_model_config() -> AgentModelConfigResponse:
    """Get the effective model assignment for every agent."""
    provider = get_llm_provider()
    doc = await get_stores().mongodb.get_agent_model_config()
    return AgentModelConfigResponse(
        models=provider.get_all_model_strings(),
        defaults=DEFAULT_AGENT_MODELS,
        updated_at=doc.get("updated_at") if doc else None,
    )


@router.put("", response_model=AgentModelConfigResponse)
async def update_model_config(req: UpdateModelsRequest) -> AgentModelConfigResponse:
    """Update model assignments for one or more agents."""
    # Validate agent names
    for name in req.models:
        if name not in AGENT_NAMES:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown agent '{name}'. Valid agents: {AGENT_NAMES}",
            )

    # Validate model strings
    for name, model_str in req.models.items():
        error = validate_model_string(model_str)
        if error:
            raise HTTPException(status_code=422, detail=error)

    # Merge with existing config
    store = get_stores().mongodb
    doc = await store.get_agent_model_config()
    existing = doc.get("models", {}) if doc else {}
    merged = {**existing, **req.models}

    await store.save_agent_model_config(merged)

    # Reload provider cache
    provider = get_llm_provider()
    provider.reload(merged)

    logger.info("Model config updated: %s", list(req.models.keys()))

    updated_doc = await store.get_agent_model_config()
    return AgentModelConfigResponse(
        models=provider.get_all_model_strings(),
        defaults=DEFAULT_AGENT_MODELS,
        updated_at=updated_doc.get("updated_at") if updated_doc else None,
    )


@router.get("/available", response_model=AvailableModelsResponse)
async def get_available_models() -> AvailableModelsResponse:
    """List all models available for assignment."""
    health = await check_ollama_health()

    # Build Ollama models list with ollama_chat/ prefix
    ollama_models: list[str] = []
    if health["connected"]:
        ollama_models = [f"ollama_chat/{m}" for m in health["models"]]
    elif KNOWN_OLLAMA_MODELS:
        # Show known models even if disconnected (they'll fallback at runtime)
        ollama_models = [f"ollama_chat/{m}" for m in KNOWN_OLLAMA_MODELS]

    return AvailableModelsResponse(
        gemini=KNOWN_GEMINI_MODELS,
        ollama=ollama_models,
        ollama_connected=health["connected"],
    )


@router.post("/preset", response_model=AgentModelConfigResponse)
async def apply_preset(req: PresetRequest) -> AgentModelConfigResponse:
    """Apply a predefined model configuration preset."""
    preset_map = MODEL_PRESETS.get(req.preset)
    if preset_map is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown preset '{req.preset}'. Valid presets: {list(MODEL_PRESETS.keys())}",
        )

    store = get_stores().mongodb
    await store.save_agent_model_config(preset_map)

    provider = get_llm_provider()
    provider.reload(preset_map)

    logger.info("Applied model preset: %s", req.preset)

    updated_doc = await store.get_agent_model_config()
    return AgentModelConfigResponse(
        models=provider.get_all_model_strings(),
        defaults=DEFAULT_AGENT_MODELS,
        updated_at=updated_doc.get("updated_at") if updated_doc else None,
    )
