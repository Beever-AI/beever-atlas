"""End-to-end integration tests for the citation registry pipeline.

Exercises the full path: decorator annotates tool output → LLM (simulated)
emits tags → rewriter rewrites to [N] → registry finalizes envelope →
persistence shim round-trips.
"""

from __future__ import annotations

import json

import pytest

from beever_atlas.agents.citations.persistence import (
    as_legacy_items,
    upgrade_envelope,
)
from beever_atlas.agents.citations.registry import bind, reset
from beever_atlas.agents.citations.permalink_resolver import (
    default_resolver,
    reset_warn_cache,
)
from beever_atlas.agents.query.stream_rewriter import StreamRewriter
from beever_atlas.agents.tools._citation_decorator import cite_tool_output


@pytest.mark.asyncio
async def test_full_pipeline_channel_messages():
    """Fake two tools firing, LLM draft with tags, verify final envelope."""

    @cite_tool_output(kind="channel_message")
    async def fake_search_channel_facts() -> list[dict]:
        return [
            {
                "text": "team picked dark theme",
                "author": "alice",
                "author_id": "U1",
                "channel_id": "C1",
                "channel_name": "design",
                "platform": "slack",
                "message_ts": "1712500000.001100",
                "workspace_domain": "beever",
                "fact_id": "f1",
                "confidence": 0.9,
            },
            {
                "text": "launch on friday",
                "author": "bob",
                "channel_id": "C1",
                "channel_name": "design",
                "platform": "slack",
                "message_ts": "1712600000.001200",
                "workspace_domain": "beever",
                "fact_id": "f2",
                "confidence": 0.7,
            },
        ]

    @cite_tool_output(kind="media")
    async def fake_search_media() -> list[dict]:
        return [
            {
                "text": "dark theme mockup attached",
                "author": "alice",
                "channel_id": "C1",
                "channel_name": "design",
                "platform": "slack",
                "message_ts": "1712500000.001100",
                "media_urls": ["https://files/mockup.png"],
                "media_type": "image",
                "fact_id": "f1",  # same underlying fact → dedup
            },
        ]

    reset_warn_cache()
    r, tok = bind()
    try:
        r.set_permalink_resolver(default_resolver)

        facts = await fake_search_channel_facts()
        media = await fake_search_media()

        # Simulate the LLM weaving tags into its response.
        answer_draft = (
            f"The team picked dark theme {facts[0]['_cite']}. "
            f"Here's the mockup {media[0]['_cite']} inline]."
            # Note: the media fact has the same source_id as facts[0]
            # (same native identity) — dedup should collapse them.
        )
        # Actually, to exercise an inline form we construct it explicitly:
        answer_draft = (
            f"Dark theme {facts[0]['_cite']} was chosen. "
            f"See mockup [src:{media[0]['_src_id']} inline] for design. "
            f"Launch {facts[1]['_cite']} on friday."
        )

        # Rewrite
        rewriter = StreamRewriter(r)
        rewritten = rewriter.feed(answer_draft) + rewriter.flush()

        env = r.finalize(rewritten)
    finally:
        reset(tok)

    # Each tool produces a distinct source (kind is part of the source_id).
    # channel_message × 2 (alice, bob) + media × 1 = 3 referenced sources.
    assert len(env.sources) == 3
    assert rewritten == (
        "Dark theme [1] was chosen. See mockup [2] for design. Launch [3] on friday."
    )

    # Inline applied only to the media-kind source (which has attachments).
    ref2 = [r for r in env.refs if r.marker == 2][0]
    assert ref2.inline is True

    # Non-inline markers keep inline=False.
    assert [r for r in env.refs if r.marker == 1][0].inline is False
    assert [r for r in env.refs if r.marker == 3][0].inline is False

    # Permalinks resolved via default_resolver for slack channel_messages.
    ref1 = [r for r in env.refs if r.marker == 1][0]
    src1 = [s for s in env.sources if s.id == ref1.source_id][0]
    assert src1.permalink is not None
    assert "slack.com/archives/C1/p" in src1.permalink

    # Media source carries the image attachment.
    src2 = [s for s in env.sources if s.id == ref2.source_id][0]
    assert any(a.kind == "image" for a in src2.attachments)


def test_envelope_persistence_round_trip():
    # Simulate what ask.py writes.
    envelope = {
        "items": [{"number": "1", "author": "alice", "channel": "design"}],
        "sources": [{"id": "src_abc", "kind": "channel_message"}],
        "refs": [{"marker": 1, "source_id": "src_abc", "inline": False}],
    }

    # Persist as JSON (QAHistoryStore path) and read back.
    persisted = json.dumps(envelope)
    loaded = json.loads(persisted)
    assert as_legacy_items(loaded) == envelope["items"]

    # Also the ChatHistoryStore path (dict passthrough).
    assert upgrade_envelope(loaded) == envelope


def test_envelope_backward_compat_with_legacy_list():
    legacy = [{"number": "1", "author": "alice"}]
    env = upgrade_envelope(legacy)
    assert env == {"items": legacy, "sources": [], "refs": []}
    # And legacy consumers still see their list
    assert as_legacy_items(env) == legacy
    assert as_legacy_items(legacy) == legacy  # raw passthrough
