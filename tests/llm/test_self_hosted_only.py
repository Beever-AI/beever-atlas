"""RES-944 — no-cloud mode. Setting LLM_FAST_MODEL alone did NOT redirect the ADK
agents because ``DEFAULT_AGENT_MODELS`` hardcodes ``gemini-*`` and shadows the env
config. ``llm_self_hosted_only`` bypasses that map and fails closed if any agent
still resolves to a cloud model.

Unit tests over the model-string resolution + guard only (no LiteLlm construction,
no network). End-to-end "no google.genai traffic on the live box" is an operational
check, not run here.
"""

from __future__ import annotations

import pytest

from beever_atlas.infra.config import ConfigurationError, Settings
from beever_atlas.llm.provider import LLMProvider, _is_cloud_model


def _provider(**overrides) -> LLMProvider:
    settings = Settings(**overrides)
    return LLMProvider(settings)


# ── _is_cloud_model ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "model,is_cloud",
    [
        ("gemini-2.5-flash", True),
        ("gemini/gemini-2.5-flash", True),
        ("openai/gemini-2.5-flash", True),  # OpenAI-compat shim still pointing at Google
        ("vertex_ai/gemini-1.5-pro", True),
        ("google/gemini-pro", True),
        ("openai/vllm-qwen", False),
        ("ollama_chat/qwen2.5", False),
        ("", False),
    ],
)
def test_is_cloud_model(model: str, is_cloud: bool) -> None:
    assert _is_cloud_model(model) is is_cloud


# ── default map bypass ─────────────────────────────────────────────────────
def test_default_map_shadows_env_when_cloud_mode_off() -> None:
    # The documented bug: LLM_FAST_MODEL is ignored for the extractors because
    # DEFAULT_AGENT_MODELS pins them to gemini.
    p = _provider(llm_fast_model="openai/vllm-qwen", llm_self_hosted_only=False)
    assert p.get_model_string("fact_extractor") == "gemini-2.5-flash"


def test_self_hosted_only_bypasses_default_map() -> None:
    p = _provider(llm_fast_model="openai/vllm-qwen", llm_self_hosted_only=True)
    assert p.get_model_string("fact_extractor") == "openai/vllm-qwen"
    assert p.get_model_string("entity_extractor") == "openai/vllm-qwen"


def test_explicit_assignment_still_wins_in_cloud_mode() -> None:
    p = _provider(llm_fast_model="openai/vllm-qwen", llm_self_hosted_only=True)
    p._agent_overrides["fact_extractor"] = "openai/some-other-selfhosted"
    assert p.get_model_string("fact_extractor") == "openai/some-other-selfhosted"


# ── fail-closed guard ──────────────────────────────────────────────────────
def test_resolve_model_raises_when_cloud_model_in_nocloud_mode() -> None:
    # Operator turned on no-cloud but left LLM_FAST_MODEL at its gemini default.
    p = _provider(llm_fast_model="gemini-2.5-flash", llm_self_hosted_only=True)
    with pytest.raises(ConfigurationError, match="cloud model"):
        p.resolve_model("fact_extractor")


def test_assert_self_hosted_only_flags_offending_agents() -> None:
    p = _provider(llm_fast_model="gemini-2.5-flash", llm_self_hosted_only=True)
    with pytest.raises(ConfigurationError, match="fact_extractor"):
        p.assert_self_hosted_only()


def test_assert_self_hosted_only_passes_when_all_self_hosted() -> None:
    p = _provider(llm_fast_model="openai/vllm-qwen", llm_self_hosted_only=True)
    p.assert_self_hosted_only()  # must not raise


def test_assert_self_hosted_only_is_noop_when_flag_off() -> None:
    p = _provider(llm_fast_model="gemini-2.5-flash", llm_self_hosted_only=False)
    p.assert_self_hosted_only()  # cloud is fine when the flag is off → no raise
