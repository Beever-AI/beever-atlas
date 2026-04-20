"""Tests for per-agent model resolution and configuration."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from beever_atlas.infra.config import Settings
from beever_atlas.llm.model_resolver import (
    DEFAULT_AGENT_MODELS,
    AGENT_NAMES,
    MODEL_PRESETS,
    is_ollama_model,
    resolve_model_object,
    validate_model_string,
)
from beever_atlas.llm.provider import LLMProvider


# ── Model Resolver Unit Tests ────────────────────────────────────────────


class TestModelResolver:
    def test_gemini_model_passthrough(self):
        result = resolve_model_object("gemini-2.5-flash")
        assert result == "gemini-2.5-flash"

    def test_gemini_lite_passthrough(self):
        result = resolve_model_object("gemini-2.5-flash-lite")
        assert result == "gemini-2.5-flash-lite"

    def test_ollama_model_wrapping(self):
        with patch.dict("os.environ", {}, clear=False):
            result = resolve_model_object("ollama_chat/gemma4:e2b")
            assert hasattr(result, "model")  # LiteLlm instance
            assert "gemma4:e2b" in str(result.model)

    def test_is_ollama_model(self):
        assert is_ollama_model("ollama_chat/gemma4:e2b") is True
        assert is_ollama_model("gemini-2.5-flash") is False

    def test_validate_model_string_valid(self):
        assert validate_model_string("gemini-2.5-flash") is None
        assert validate_model_string("gemini-2.5-flash-lite") is None
        assert validate_model_string("ollama_chat/gemma4:e2b") is None
        assert validate_model_string("ollama_chat/gemma4:e4b") is None

    def test_validate_model_string_invalid(self):
        err = validate_model_string("gpt-4o")
        assert err is not None
        assert "Invalid model" in err

    def test_default_map_covers_all_agents(self):
        for name in AGENT_NAMES:
            assert name in DEFAULT_AGENT_MODELS, f"Missing default for {name}"

    def test_presets_cover_all_agents(self):
        for preset_name, preset_map in MODEL_PRESETS.items():
            for name in AGENT_NAMES:
                assert name in preset_map, f"Preset '{preset_name}' missing {name}"


# ── LLMProvider resolve_model Tests ──────────────────────────────────────


class TestLLMProviderResolve:
    def _make_provider(self, **overrides) -> LLMProvider:
        defaults: dict[str, object] = dict(
            google_api_key="test",
            llm_fast_model="gemini-2.5-flash",
            llm_quality_model="gemini-2.5-flash",
            ollama_enabled=False,
        )
        defaults.update(overrides)
        settings = Settings(**defaults)  # type: ignore[arg-type]
        return LLMProvider(settings)

    def test_resolve_from_default_map(self):
        provider = self._make_provider()
        # fact_extractor defaults to gemini-2.5-flash
        result = provider.resolve_model("fact_extractor")
        assert result == "gemini-2.5-flash"

    def test_resolve_lite_agent_from_default(self):
        # An agent not in DEFAULT_AGENT_MODELS falls back to llm_fast_model.
        provider = self._make_provider(llm_fast_model="gemini-2.5-flash-lite")
        result = provider.resolve_model("classifier")
        assert result == "gemini-2.5-flash-lite"

    def test_resolve_mongodb_override_takes_precedence(self):
        provider = self._make_provider()
        provider.reload({"fact_extractor": "gemini-2.5-flash-lite"})
        result = provider.resolve_model("fact_extractor")
        assert result == "gemini-2.5-flash-lite"

    def test_resolve_unknown_agent_falls_to_env(self):
        provider = self._make_provider()
        result = provider.resolve_model("unknown_agent")
        assert result == "gemini-2.5-flash"

    def test_resolve_ollama_falls_back_when_disabled(self):
        provider = self._make_provider(ollama_enabled=False)
        # image_describer defaults to ollama_chat/gemma4:e2b
        result = provider.resolve_model("image_describer")
        # Should fall back since ollama_enabled=False
        assert result == "gemini-2.5-flash-lite"

    def test_get_all_model_strings(self):
        provider = self._make_provider()
        all_models = provider.get_all_model_strings()
        assert len(all_models) == len(AGENT_NAMES)
        for name in AGENT_NAMES:
            assert name in all_models

    def test_reload_updates_overrides(self):
        # Unknown agents fall back to llm_fast_model when no override is set.
        provider = self._make_provider(llm_fast_model="gemini-2.5-flash-lite")
        provider.reload({"classifier": "gemini-2.5-flash"})
        assert provider.get_model_string("classifier") == "gemini-2.5-flash"
        provider.reload({})
        assert provider.get_model_string("classifier") == "gemini-2.5-flash-lite"


# ── Model Config API Tests ───────────────────────────────────────────────


class TestModelConfigAPI:
    @pytest.mark.asyncio
    async def test_get_model_config(self):
        from beever_atlas.api.models import get_model_config

        mock_store = MagicMock()
        mock_store.mongodb.get_agent_model_config = AsyncMock(return_value=None)

        with (
            patch("beever_atlas.api.models.get_stores", return_value=mock_store),
            patch("beever_atlas.api.models.get_llm_provider") as mock_provider,
        ):
            provider = self._mock_provider()
            mock_provider.return_value = provider

            result = await get_model_config()
            assert result.defaults == DEFAULT_AGENT_MODELS
            assert len(result.models) == len(AGENT_NAMES)

    @pytest.mark.asyncio
    async def test_update_rejects_invalid_agent(self):
        from beever_atlas.api.models import update_model_config, UpdateModelsRequest
        from fastapi import HTTPException

        req = UpdateModelsRequest(models={"nonexistent_agent": "gemini-2.5-flash"})
        with pytest.raises(HTTPException) as exc_info:
            await update_model_config(req)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_update_rejects_invalid_model(self):
        from beever_atlas.api.models import update_model_config, UpdateModelsRequest
        from fastapi import HTTPException

        req = UpdateModelsRequest(models={"classifier": "gpt-4o"})
        with pytest.raises(HTTPException) as exc_info:
            await update_model_config(req)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_apply_preset_rejects_unknown(self):
        from beever_atlas.api.models import apply_preset, PresetRequest
        from fastapi import HTTPException

        req = PresetRequest(preset="nonexistent")
        with pytest.raises(HTTPException) as exc_info:
            await apply_preset(req)
        assert exc_info.value.status_code == 422

    @staticmethod
    def _mock_provider() -> LLMProvider:
        settings = Settings(
            google_api_key="test",
            llm_fast_model="gemini-2.5-flash",
            llm_quality_model="gemini-2.5-flash",
            ollama_enabled=False,
        )
        return LLMProvider(settings)
