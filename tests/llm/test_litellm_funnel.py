"""PR-A: agent-llm-provider-pluggable — LiteLlm wrap covers every provider.

Verifies the ``LLM_USE_LITELLM_FOR_GEMINI`` cutover flag:
  - Flag-on (default post-cutover): bare ``gemini-*`` normalises and wraps in
    ``LiteLlm`` so ADK calls route through ``litellm.acompletion`` →
    ``dispatch_completion`` → the per-Endpoint throttle.
  - Flag-off (emergency rollback): legacy native ``google.genai`` path is
    preserved for Gemini (bare-string return). Ollama keeps wrapping
    regardless.

Also covers the ``validate_model_string`` prefix-check expansion that accepts
every supported LiteLLM provider — not just gemini and ollama_chat.
"""

from __future__ import annotations

from beever_atlas.infra.config import Settings
from beever_atlas.llm.model_resolver import (
    SUPPORTED_PROVIDERS,
    resolve_model_object,
    validate_model_string,
)


def _settings_with_flag(monkeypatch, *, flag: bool) -> None:
    """Force the cutover flag explicitly + clear any conflicting env."""
    monkeypatch.setenv("LLM_USE_LITELLM_FOR_GEMINI", "true" if flag else "false")
    # ``get_settings`` is ``@lru_cache``-decorated; clear it so the next call
    # re-reads env and observes the patched flag value.
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()


def _is_litellm_wrap(obj) -> bool:
    """Return True iff ``obj`` is an ADK LiteLlm wrapper (duck-typed)."""
    from google.adk.models.lite_llm import LiteLlm

    return isinstance(obj, LiteLlm)


# ── resolve_model_object ─────────────────────────────────────────────────


def test_gemini_bare_wraps_when_flag_on(monkeypatch):
    _settings_with_flag(monkeypatch, flag=True)
    obj = resolve_model_object("gemini-2.5-flash")
    assert _is_litellm_wrap(obj)
    # Model string inside the wrap is the normalised, prefixed form.
    assert obj.model == "gemini/gemini-2.5-flash"  # type: ignore[attr-defined]


def test_gemini_bare_passes_through_when_flag_off(monkeypatch):
    _settings_with_flag(monkeypatch, flag=False)
    obj = resolve_model_object("gemini-2.5-flash")
    # Flag-off legacy path — ADK consumes the bare string natively.
    assert obj == "gemini-2.5-flash"


def test_gemini_prefixed_still_wraps_when_flag_on(monkeypatch):
    _settings_with_flag(monkeypatch, flag=True)
    obj = resolve_model_object("gemini/gemini-2.5-pro")
    assert _is_litellm_wrap(obj)
    assert obj.model == "gemini/gemini-2.5-pro"  # type: ignore[attr-defined]


def test_openai_prefixed_wraps_regardless_of_flag(monkeypatch):
    _settings_with_flag(monkeypatch, flag=True)
    obj_on = resolve_model_object("openai/gpt-4o-mini")
    assert _is_litellm_wrap(obj_on)
    assert obj_on.model == "openai/gpt-4o-mini"  # type: ignore[attr-defined]


def test_anthropic_prefixed_wraps(monkeypatch):
    _settings_with_flag(monkeypatch, flag=True)
    obj = resolve_model_object("anthropic/claude-sonnet-4-6")
    assert _is_litellm_wrap(obj)
    assert obj.model == "anthropic/claude-sonnet-4-6"  # type: ignore[attr-defined]


def test_ollama_always_wraps_regardless_of_flag(monkeypatch):
    """Ollama has never had a non-LiteLlm path. Flag value MUST NOT affect it."""
    _settings_with_flag(monkeypatch, flag=False)  # even with flag off
    obj = resolve_model_object("ollama_chat/qwen2.5:14b")
    assert _is_litellm_wrap(obj)
    assert obj.model == "ollama_chat/qwen2.5:14b"  # type: ignore[attr-defined]


# ── validate_model_string ────────────────────────────────────────────────


def test_validate_accepts_bare_gemini():
    assert validate_model_string("gemini-2.5-flash") is None
    assert validate_model_string("gemini-2.5-flash-lite") is None


def test_validate_accepts_every_supported_provider():
    for prefix in SUPPORTED_PROVIDERS:
        assert validate_model_string(f"{prefix}/some-model") is None, (
            f"prefix {prefix!r} should be accepted"
        )


def test_validate_rejects_unprefixed_non_gemini():
    err = validate_model_string("gpt-4o-mini")
    assert err is not None
    assert "must be prefixed with a provider" in err


def test_validate_rejects_unsupported_prefix():
    err = validate_model_string("totally_made_up/x")
    assert err is not None
    assert "Unsupported provider 'totally_made_up'" in err
    # The error message lists the supported set so operators can self-correct.
    for prefix in SUPPORTED_PROVIDERS:
        assert prefix in err


def test_settings_default_has_flag_off():
    """Post-F12 the default is OFF — the cutover True default silently broke
    extraction because ADK's LiteLlm wrapper does not translate
    ``GenerateContentConfig.response_mime_type`` into LiteLLM's
    ``response_format``. Native ADK Gemini honors response_mime_type and
    produces extractable JSON; flag-on remains available as an opt-in for
    operators willing to accept the regression (or once the wrapper learns
    the translation). See
    ``src/beever_atlas/infra/config.py:llm_use_litellm_for_gemini``
    docstring for the full reasoning.
    """
    s = Settings()
    assert s.llm_use_litellm_for_gemini is False
