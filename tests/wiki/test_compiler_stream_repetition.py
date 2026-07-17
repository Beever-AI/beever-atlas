"""Regression tests for RES-947 — a streamed wiki page compile that litellm aborts
for chunk-repetition (dense OCR ``---`` separator walls) must NOT crash the page
("subtopic page failed hard"). ``_llm_generate_json`` retries the call once
non-streamed (litellm's repetition guard only runs in the streaming handler), so
the full content flows to the existing degenerate guard instead of raising.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beever_atlas.wiki import compiler as compiler_mod
from beever_atlas.wiki.compiler import WikiCompiler, _is_stream_repetition_abort


def _settings(**over):
    base = dict(
        wiki_token_budget_v2=False,
        wiki_compiler_v2=False,  # JSON mode (not delimited)
        wiki_llm_streaming=True,
        ollama_api_base="http://localhost:11434",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _resp(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_is_stream_repetition_abort_detects_by_message() -> None:
    assert _is_stream_repetition_abort(
        RuntimeError("litellm.MidStreamFallbackError: The model is repeating the same chunk = ----")
    )
    assert not _is_stream_repetition_abort(ValueError("some other error"))


@pytest.mark.asyncio
async def test_llm_generate_json_retries_non_streamed_on_repetition_abort(monkeypatch) -> None:
    """Streamed call aborts with a repetition error → retried non-streamed → returns
    the good content, and dispatch is called twice (stream=True then stream=False)."""
    calls: list[bool] = []

    async def _fake_dispatch(**kwargs):
        calls.append(bool(kwargs.get("stream")))
        if kwargs.get("stream"):
            raise RuntimeError(
                "litellm.MidStreamFallbackError: The model is repeating the same chunk = ------"
            )
        return _resp('{"content": "real prose", "summary": "s"}')

    monkeypatch.setattr(
        "beever_atlas.services.llm_dispatch.dispatch_completion", _fake_dispatch
    )
    monkeypatch.setattr(compiler_mod, "get_settings", _settings, raising=False)
    monkeypatch.setattr("beever_atlas.infra.config.get_settings", _settings, raising=False)

    wc = object.__new__(WikiCompiler)
    wc._model_name = "gemini-2.5-flash"

    out = await wc._llm_generate_json("prompt", page_kind="topic")

    assert out == '{"content": "real prose", "summary": "s"}'
    assert calls == [True, False], "must try streamed first, then retry non-streamed"


@pytest.mark.asyncio
async def test_llm_generate_json_reraises_non_repetition_errors(monkeypatch) -> None:
    """A non-repetition error is NOT swallowed — it propagates unchanged."""

    async def _fake_dispatch(**_kwargs):
        raise ValueError("genuine upstream failure")

    monkeypatch.setattr(
        "beever_atlas.services.llm_dispatch.dispatch_completion", _fake_dispatch
    )
    monkeypatch.setattr("beever_atlas.infra.config.get_settings", _settings, raising=False)

    wc = object.__new__(WikiCompiler)
    wc._model_name = "gemini-2.5-flash"

    with pytest.raises(ValueError, match="genuine upstream failure"):
        await wc._llm_generate_json("prompt", page_kind="topic")


@pytest.mark.asyncio
async def test_llm_generate_json_no_stream_does_not_retry(monkeypatch) -> None:
    """When streaming is already off, a repetition abort is not retried (re-raised)."""
    calls: list[bool] = []

    async def _fake_dispatch(**kwargs):
        calls.append(bool(kwargs.get("stream")))
        raise RuntimeError("The model is repeating the same chunk = ----")

    monkeypatch.setattr(
        "beever_atlas.services.llm_dispatch.dispatch_completion", _fake_dispatch
    )
    monkeypatch.setattr("beever_atlas.infra.config.get_settings", _settings, raising=False)

    wc = object.__new__(WikiCompiler)
    wc._model_name = "gemini-2.5-flash"

    with pytest.raises(RuntimeError):
        await wc._llm_generate_json("prompt", page_kind="topic", stream_override=False)
    assert calls == [False], "no non-streamed retry when already non-streamed"
