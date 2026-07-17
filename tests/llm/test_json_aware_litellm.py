"""RES-944 — extraction must keep JSON-mode output when routed to a self-hosted
OpenAI-compatible endpoint (vLLM/Qwen), instead of silently degrading to prose.

ADK's ``LiteLlm`` builds ``response_format`` only from ``response_schema`` and
ignores ``response_mime_type='application/json'`` (which the fact/entity
extractors set). ``JsonAwareLiteLlm`` bakes ``response_format={'type':
'json_object'}`` into the wrapper so the no-cloud path emits structured JSON.

These are unit tests over model construction only. End-to-end proof (facts > 0
on live Qwen) requires a rebuilt POC stack and is intentionally NOT run here —
it must run on the POC box, never against prod.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from google.adk.models.lite_llm import LiteLlm

from beever_atlas.llm import model_resolver
from beever_atlas.llm.model_resolver import (
    _json_aware_litellm_cls,
    resolve_model_object,
)


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, use_litellm_for_gemini: bool) -> None:
    fake = SimpleNamespace(
        llm_use_litellm_for_gemini=use_litellm_for_gemini,
        ollama_api_base="http://localhost:11434",
    )
    monkeypatch.setattr(model_resolver, "get_settings", lambda: fake)


def test_json_aware_class_is_a_litellm_subclass() -> None:
    cls = _json_aware_litellm_cls()
    assert issubclass(cls, LiteLlm)
    # Memoised — same class object across calls (lazy singleton).
    assert cls is _json_aware_litellm_cls()


def test_json_aware_bakes_response_format() -> None:
    obj = _json_aware_litellm_cls()(model="openai/vllm-qwen")
    assert obj._additional_args["response_format"] == {"type": "json_object"}


def test_explicit_response_format_is_not_overridden() -> None:
    # setdefault → a caller-supplied response_format wins.
    obj = _json_aware_litellm_cls()(
        model="openai/vllm-qwen", response_format={"type": "text"}
    )
    assert obj._additional_args["response_format"] == {"type": "text"}


@pytest.mark.parametrize("flag", [True, False])
def test_vllm_path_forces_json_object_regardless_of_gemini_flag(
    monkeypatch: pytest.MonkeyPatch, flag: bool
) -> None:
    # A self-hosted vLLM/Qwen endpoint is non-Gemini, so it wraps in LiteLlm on
    # both the cutover-on and flag-off paths. force_json_object must make it the
    # JSON-aware subclass either way.
    _patch_settings(monkeypatch, use_litellm_for_gemini=flag)
    obj = resolve_model_object(
        "openai/vllm-qwen", api_base="https://vllm.votee.dev/v1", force_json_object=True
    )
    assert isinstance(obj, _json_aware_litellm_cls())
    assert obj._additional_args["response_format"] == {"type": "json_object"}
    assert obj._additional_args["api_base"] == "https://vllm.votee.dev/v1"


def test_without_force_plain_litellm_has_no_forced_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch, use_litellm_for_gemini=True)
    obj = resolve_model_object("openai/vllm-qwen", force_json_object=False)
    assert isinstance(obj, LiteLlm)
    assert not isinstance(obj, _json_aware_litellm_cls())
    assert "response_format" not in obj._additional_args


def test_native_gemini_bare_string_is_unaffected_by_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Flag OFF → bare Gemini returns as a string for ADK's native path, which
    # already honours response_mime_type. force_json_object is a no-op here.
    _patch_settings(monkeypatch, use_litellm_for_gemini=False)
    result = resolve_model_object("gemini-2.5-flash", force_json_object=True)
    assert result == "gemini-2.5-flash"
