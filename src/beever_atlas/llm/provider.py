"""Centralized LLM model selection with per-agent configuration."""

from __future__ import annotations

import logging
from typing import Any

from beever_atlas.infra.config import Settings
from beever_atlas.llm.model_resolver import (
    DEFAULT_AGENT_MODELS,
    is_ollama_model,
    resolve_model_object,
)

logger = logging.getLogger(__name__)

_MODEL_ALIASES: dict[str, str] = {
    # Gemini 2.0 Flash Lite is retired for new users.
    "gemini-2.0-flash-lite": "gemini-2.5-flash-lite-preview-06-17",
    "gemini/gemini-2.0-flash-lite": "gemini-2.5-flash-lite-preview-06-17",
    # Keep older fast/quality defaults working across existing local .env files.
    "gemini-2.0-flash": "gemini-2.5-flash",
    "gemini/gemini-2.0-flash": "gemini-2.5-flash",
}

# Ollama fallback model when local service is unreachable
_OLLAMA_FALLBACK = "gemini-2.5-flash-lite"


class LLMProvider:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._logged_deprecations: set[str] = set()
        # Per-agent model overrides loaded from MongoDB (empty until reload)
        self._agent_overrides: dict[str, str] = {}
        self._ollama_available: bool | None = None  # cached health status

    def _resolve_alias(self, model: str, context: str) -> str:
        resolved = _MODEL_ALIASES.get(model, model)
        if resolved != model:
            logger.warning(
                "LLMProvider: remapping deprecated model %s -> %s for %s",
                model,
                resolved,
                context,
            )
        return resolved

    def get_model(self, tier: str = "fast") -> str:
        if tier == "fast":
            model = self._settings.llm_fast_model
        elif tier == "quality":
            model = self._settings.llm_quality_model
        else:
            raise ValueError(f"Unknown tier: {tier}")
        return self._resolve_alias(model, f"tier={tier}")

    def resolve_model(self, agent_name: str) -> Any:
        """Resolve the model for a specific agent.

        Priority: MongoDB override → default map → LLM_FAST_MODEL env var.
        Returns a string (Gemini) or LiteLlm instance (Ollama).
        """
        # 1. Check MongoDB overrides
        model_str = self._agent_overrides.get(agent_name)
        # 2. Fall back to default map
        if not model_str:
            model_str = DEFAULT_AGENT_MODELS.get(agent_name)
        # 3. Fall back to env var
        if not model_str:
            model_str = self._settings.llm_fast_model

        model_str = self._resolve_alias(model_str, f"agent={agent_name}")

        # Ollama fallback: if model is Ollama but service is unreachable
        if is_ollama_model(model_str):
            if not self._check_ollama_cached():
                logger.warning(
                    "LLMProvider: Ollama unreachable for agent '%s', falling back to '%s'",
                    agent_name,
                    _OLLAMA_FALLBACK,
                )
                return _OLLAMA_FALLBACK

        return resolve_model_object(model_str)

    def get_model_string(self, agent_name: str) -> str:
        """Get the raw model string for an agent (without LiteLlm wrapping).

        Useful for API responses and display.
        """
        model_str = self._agent_overrides.get(agent_name)
        if not model_str:
            model_str = DEFAULT_AGENT_MODELS.get(agent_name)
        if not model_str:
            model_str = self._settings.llm_fast_model
        return self._resolve_alias(model_str, f"agent={agent_name}")

    def get_all_model_strings(self) -> dict[str, str]:
        """Get the effective model string for every known agent."""
        from beever_atlas.llm.model_resolver import AGENT_NAMES

        return {name: self.get_model_string(name) for name in AGENT_NAMES}

    def _check_ollama_cached(self) -> bool:
        """Check Ollama availability with simple caching."""
        if self._ollama_available is not None:
            return self._ollama_available
        if not self._settings.ollama_enabled:
            self._ollama_available = False
            return False
        try:
            import httpx

            resp = httpx.get(
                f"{self._settings.ollama_api_base}/api/tags",
                timeout=3,
            )
            self._ollama_available = resp.status_code == 200
        except Exception:
            self._ollama_available = False
        return self._ollama_available

    def reload(self, overrides: dict[str, str] | None = None) -> None:
        """Refresh per-agent model overrides.

        Args:
            overrides: If provided, use directly. Otherwise caller should
                       pass data from MongoDB.
        """
        if overrides is not None:
            self._agent_overrides = dict(overrides)
        # Reset Ollama cache so next resolve re-checks
        self._ollama_available = None
        logger.info(
            "LLMProvider: reloaded with %d agent overrides",
            len(self._agent_overrides),
        )

    async def reload_from_db(self) -> None:
        """Load per-agent model config from MongoDB."""
        try:
            from beever_atlas.stores import get_stores

            doc = await get_stores().mongodb.get_agent_model_config()
            overrides = doc.get("models", {}) if doc else {}
            self.reload(overrides)
        except Exception:
            logger.warning("LLMProvider: failed to load model config from MongoDB", exc_info=True)

    @property
    def fast(self) -> str:
        return self.get_model("fast")

    @property
    def quality(self) -> str:
        return self.get_model("quality")

    @property
    def embedding_model(self) -> str:
        return self._settings.jina_model

    @property
    def embedding_dimensions(self) -> int:
        return self._settings.jina_dimensions


_provider: LLMProvider | None = None


def _validate_model_resolution(provider: LLMProvider) -> None:
    """Fail fast when configured ADK models cannot be resolved.

    This catches missing/incompatible LiteLLM installations and invalid model
    names during app startup instead of during background sync jobs.
    """
    from google.adk.models.registry import LLMRegistry

    for tier, model_name in (
        ("fast", provider.fast),
        ("quality", provider.quality),
    ):
        try:
            LLMRegistry.resolve(model_name)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Invalid LLM config: tier=%s model=%s cannot be resolved by ADK. "
                "Ensure LiteLLM is installed (litellm>=1.75.5) and model names are valid."
                % (tier, model_name)
            ) from exc
        logger.info("LLMProvider: validated tier=%s model=%s", tier, model_name)


def init_llm_provider(settings: Settings) -> None:
    global _provider
    provider = LLMProvider(settings)
    _validate_model_resolution(provider)
    _provider = provider


def get_llm_provider() -> LLMProvider:
    if _provider is None:
        raise RuntimeError(
            "LLM provider not initialized. Call init_llm_provider() during app startup."
        )
    return _provider
