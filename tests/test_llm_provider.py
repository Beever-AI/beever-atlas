from __future__ import annotations

from beever_atlas.infra.config import Settings
from beever_atlas.llm.provider import LLMProvider


def test_deprecated_flash_lite_model_is_remapped() -> None:
    settings = Settings(
        llm_fast_model="gemini-2.0-flash-lite",
        llm_quality_model="gemini-2.5-pro",
    )
    provider = LLMProvider(settings)
    assert provider.fast == "gemini-2.5-flash"


def test_provider_style_deprecated_flash_lite_model_is_remapped() -> None:
    settings = Settings(
        llm_fast_model="gemini/gemini-2.0-flash-lite",
        llm_quality_model="gemini-2.5-pro",
    )
    provider = LLMProvider(settings)
    assert provider.fast == "gemini-2.5-flash"


def test_deprecated_flash_model_is_remapped() -> None:
    settings = Settings(
        llm_fast_model="gemini-2.5-flash",
        llm_quality_model="gemini-2.0-flash",
    )
    provider = LLMProvider(settings)
    assert provider.quality == "gemini-2.5-pro"
