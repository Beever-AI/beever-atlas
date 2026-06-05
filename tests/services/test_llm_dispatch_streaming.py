"""Issue #223 — streamed-completion reassembly in ``llm_dispatch``.

``_acompletion_assembled_stream`` runs ``litellm.acompletion(stream=True)`` so a
long generation keeps the socket warm (no ~130s idle disconnect), then rebuilds
the streamed chunks into the SAME response shape non-streaming callers consume.
It takes ``litellm`` as its first arg precisely so it can be tested without the
real SDK.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from beever_atlas.services.llm_dispatch import _acompletion_assembled_stream


@pytest.fixture(autouse=True)
def _fresh_settings():
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_litellm(chunks: list[str], *, build_none: bool = False):
    captured: dict = {}

    async def _acompletion(*, messages, stream, **kw):
        captured["stream"] = stream
        captured["messages"] = messages
        captured["kwargs"] = kw

        async def _gen():
            for t in chunks:
                yield t

        return _gen()

    def _builder(got_chunks, messages):
        captured["built_from"] = list(got_chunks)
        if build_none:
            return None
        assembled = MagicMock()
        assembled.choices = [MagicMock()]
        assembled.choices[0].message.content = "".join(got_chunks)
        return assembled

    fake = MagicMock()
    fake.acompletion = _acompletion
    fake.stream_chunk_builder = _builder
    return fake, captured


@pytest.mark.asyncio
async def test_streams_and_reassembles_into_one_response():
    fake, captured = _fake_litellm(["Hello ", "stream", "ed world"])

    resp = await _acompletion_assembled_stream(
        fake,
        messages=[{"role": "user", "content": "hi"}],
        model="gemini/gemini-2.5-pro",
        custom_llm_provider="gemini",
        max_tokens=32768,
    )

    # It requested streaming, collected every chunk, and rebuilt the full text.
    assert captured["stream"] is True
    assert captured["built_from"] == ["Hello ", "stream", "ed world"]
    assert resp.choices[0].message.content == "Hello streamed world"
    # Non-stream kwargs (model/provider/max_tokens) are forwarded; ``stream`` is
    # owned by the helper and not duplicated into kwargs.
    assert captured["kwargs"]["model"] == "gemini/gemini-2.5-pro"
    assert captured["kwargs"]["custom_llm_provider"] == "gemini"
    assert captured["kwargs"]["max_tokens"] == 32768
    assert "stream" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_empty_stream_raises():
    fake, _ = _fake_litellm([], build_none=True)
    with pytest.raises(RuntimeError, match="no chunks"):
        await _acompletion_assembled_stream(
            fake, messages=[], model="m", custom_llm_provider="gemini"
        )
