"""Centralized LLM model selection with tier routing."""
from __future__ import annotations

import logging

from beever_atlas.infra.config import Settings

logger = logging.getLogger(__name__)

_MODEL_ALIASES: dict[str, str] = {
    # Gemini 2.0 Flash Lite is retired for new users.
    "gemini-2.0-flash-lite": "gemini-2.5-flash-lite-preview-06-17",
    "gemini/gemini-2.0-flash-lite": "gemini-2.5-flash-lite-preview-06-17",
    # Keep older fast/quality defaults working across existing local .env files.
    "gemini-2.0-flash": "gemini-2.5-flash",
    "gemini/gemini-2.0-flash": "gemini-2.5-flash",
}


class LLMProvider:
    def __init__(self, settings: Settings):
        self._settings = settings

    def get_model(self, tier: str = "fast") -> str:
        if tier == "fast":
            model = self._settings.llm_fast_model
        elif tier == "quality":
            model = self._settings.llm_quality_model
        else:
            raise ValueError(f"Unknown tier: {tier}")
        resolved = _MODEL_ALIASES.get(model, model)
        if resolved != model:
            logger.warning(
                "LLMProvider: remapping deprecated model %s -> %s for tier=%s",
                model,
                resolved,
                tier,
            )
        return resolved

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
        raise RuntimeError("LLM provider not initialized. Call init_llm_provider() during app startup.")
    return _provider
