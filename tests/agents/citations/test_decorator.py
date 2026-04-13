"""Unit tests for the @cite_tool_output decorator.

Covers: flag-off no-op, source registration, _cite annotation, skip on
empty excerpt, per-kind attachment building, and end-to-end integration
with the rewriter.
"""

from __future__ import annotations

import asyncio

import pytest

from beever_atlas.agents.citations.registry import (
    SourceRegistry,
    bind,
    reset,
)
from beever_atlas.agents.tools._citation_decorator import (
    cite_tool_output,
    _normalize_score,
)


# ---- flag-off / no-registry behavior ----------------------------------


@pytest.mark.asyncio
async def test_no_registry_bound_returns_unmodified():
    """With no registry in context, the decorator must be a transparent no-op."""

    @cite_tool_output(kind="channel_message")
    async def fake_tool(channel_id: str, query: str) -> list[dict]:
        return [{"text": "hi", "channel_id": channel_id, "author": "a", "message_ts": "1.0"}]

    out = await fake_tool(channel_id="C1", query="q")
    assert "_cite" not in out[0]
    assert "_src_id" not in out[0]


# ---- registration + annotation ----------------------------------------


@pytest.mark.asyncio
async def test_annotation_and_registration():
    @cite_tool_output(kind="channel_message")
    async def fake_tool(channel_id: str, query: str) -> list[dict]:
        return [
            {
                "text": "hello world",
                "author": "alice",
                "channel_id": channel_id,
                "channel_name": "general",
                "platform": "slack",
                "message_ts": "1712500000.001100",
                "fact_id": "f-1",
                "confidence": 0.9,
            }
        ]

    r, tok = bind()
    try:
        out = await fake_tool(channel_id="C1", query="q")
    finally:
        reset(tok)

    assert r.registered_count == 1
    assert "_cite" in out[0]
    assert out[0]["_cite"].startswith("[src:src_")
    assert "_src_id" in out[0]
    assert out[0]["_cite"] == f"[src:{out[0]['_src_id']}]"


@pytest.mark.asyncio
async def test_skip_item_with_empty_excerpt():
    @cite_tool_output(kind="channel_message")
    async def fake_tool(channel_id: str) -> list[dict]:
        return [
            {"text": "", "channel_id": channel_id, "message_ts": "1.0"},
            {"text": "real", "channel_id": channel_id, "message_ts": "2.0"},
        ]

    r, tok = bind()
    try:
        out = await fake_tool(channel_id="C1")
    finally:
        reset(tok)

    assert r.registered_count == 1
    assert "_cite" not in out[0]
    assert "_cite" in out[1]


@pytest.mark.asyncio
async def test_channel_message_attachments_from_media_urls():
    @cite_tool_output(kind="channel_message")
    async def fake_tool() -> list[dict]:
        return [
            {
                "text": "see diagram",
                "author": "a",
                "channel_id": "C1",
                "message_ts": "1.0",
                "media_urls": ["https://x/1.png", "https://x/2.png"],
                "media_type": "image",
            }
        ]

    r, tok = bind()
    try:
        await fake_tool()
    finally:
        reset(tok)

    source = list(r._sources.values())[0]
    assert len(source.attachments) == 2
    assert source.attachments[0].kind == "image"
    assert source.attachments[0].url == "https://x/1.png"


@pytest.mark.asyncio
async def test_web_result_attachment_is_link_preview():
    @cite_tool_output(kind="web_result")
    async def fake_tool() -> list[dict]:
        return [{"text": "snippet", "url": "https://ex.com", "title": "Ex"}]

    r, tok = bind()
    try:
        await fake_tool()
    finally:
        reset(tok)

    source = list(r._sources.values())[0]
    assert len(source.attachments) == 1
    assert source.attachments[0].kind == "link_preview"
    assert source.attachments[0].title == "Ex"


@pytest.mark.asyncio
async def test_link_previews_from_channel_link_urls():
    @cite_tool_output(kind="channel_message")
    async def fake_tool() -> list[dict]:
        return [
            {
                "text": "shared a link",
                "author": "a",
                "channel_id": "C1",
                "message_ts": "1.0",
                "link_urls": ["https://a.com", "https://b.com"],
                "link_titles": ["A", "B"],
            }
        ]

    r, tok = bind()
    try:
        await fake_tool()
    finally:
        reset(tok)

    source = list(r._sources.values())[0]
    kinds = [a.kind for a in source.attachments]
    titles = [a.title for a in source.attachments]
    assert kinds == ["link_preview", "link_preview"]
    assert titles == ["A", "B"]


# ---- score normalization ----------------------------------------------


def test_score_normalization_0_to_1_passthrough():
    assert _normalize_score(0.5) == 0.5


def test_score_normalization_0_to_100_scaled():
    assert _normalize_score(80) == 0.8


def test_score_normalization_none():
    assert _normalize_score(None) is None


def test_score_normalization_invalid():
    assert _normalize_score("abc") is None


def test_score_normalization_clamps_high():
    assert _normalize_score(150) == 1.0


# ---- end-to-end with rewriter -----------------------------------------


@pytest.mark.asyncio
async def test_decorator_end_to_end_with_rewriter():
    """Simulate: tool returns data → LLM emits tag → rewriter produces [N]."""
    from beever_atlas.agents.query.stream_rewriter import StreamRewriter

    @cite_tool_output(kind="channel_message")
    async def fake_tool() -> list[dict]:
        return [
            {
                "text": "the decision",
                "author": "alice",
                "channel_id": "C1",
                "channel_name": "eng",
                "platform": "slack",
                "message_ts": "1712500000.001100",
                "workspace_domain": "beever",
            }
        ]

    r, tok = bind()
    try:
        results = await fake_tool()
        # Simulated LLM prose uses the tag it saw.
        cite = results[0]["_cite"]
        rewriter = StreamRewriter(r)
        out = rewriter.feed(f"Per {cite}, it's settled.")
        out += rewriter.flush()
        env = r.finalize()
    finally:
        reset(tok)

    assert out == "Per [1], it's settled."
    assert len(env.sources) == 1
    assert env.sources[0].permalink is None  # no resolver attached here
    assert env.refs[0].marker == 1


# ---- async fixture helpers --------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
