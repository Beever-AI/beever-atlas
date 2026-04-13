"""Unit tests for Phase 2 decorator coverage.

Verifies that wiki, external, and graph tool returns flow through the
SourceRegistry correctly with the right kinds and shapes.
"""

from __future__ import annotations

import pytest

from beever_atlas.agents.citations.registry import bind, reset
from beever_atlas.agents.tools._citation_decorator import cite_tool_output


# ---- dict-return unwrapping -------------------------------------------


@pytest.mark.asyncio
async def test_dict_with_results_list_is_unwrapped():
    @cite_tool_output(kind="web_result")
    async def tool() -> dict:
        return {
            "answer": "synthesized",
            "results": [
                {
                    "title": "A",
                    "url": "https://a.com",
                    "text": "excerpt a",
                    "score": 0.8,
                },
                {
                    "title": "B",
                    "url": "https://b.com",
                    "text": "excerpt b",
                    "score": 0.6,
                },
            ],
        }

    r, tok = bind()
    try:
        out = await tool()
    finally:
        reset(tok)

    assert "_cite" in out["results"][0]
    assert "_cite" in out["results"][1]
    assert "_cite" not in out  # outer dict untouched
    assert r.registered_count == 2


@pytest.mark.asyncio
async def test_single_source_dict_is_annotated_in_place():
    @cite_tool_output(kind="wiki_page")
    async def tool(channel_id: str, page_type: str) -> dict:
        return {
            "page_type": page_type,
            "channel_id": channel_id,
            "text": "page excerpt",
            "summary": "page excerpt",
            "content": "long content body here",
        }

    r, tok = bind()
    try:
        out = await tool(channel_id="C1", page_type="overview")
    finally:
        reset(tok)

    assert r.registered_count == 1
    assert "_cite" in out
    assert out["_cite"].startswith("[src:")
    source = list(r._sources.values())[0]
    assert source.kind == "wiki_page"
    assert source.native["channel_id"] == "C1"
    assert source.native["page_type"] == "overview"


@pytest.mark.asyncio
async def test_decision_record_from_graph_tool():
    @cite_tool_output(kind="decision_record")
    async def tool(channel_id: str, topic: str) -> list[dict]:
        return [
            {
                "entity": "proposal-v2",
                "superseded_by": "proposal-v3",
                "text": "proposal v2 superseded by v3",
                "decision_id": "C1:proposal-v2:proposal-v3",
                "channel_id": channel_id,
                "topic": topic,
            },
            {
                "entity": "proposal-v3",
                "superseded_by": "proposal-v4",
                "text": "proposal v3 superseded by v4",
                "decision_id": "C1:proposal-v3:proposal-v4",
                "channel_id": channel_id,
                "topic": topic,
            },
        ]

    r, tok = bind()
    try:
        out = await tool(channel_id="C1", topic="auth")
    finally:
        reset(tok)

    assert r.registered_count == 2
    for item in out:
        assert "_cite" in item
        assert "_src_id" in item
    kinds = {s.kind for s in r._sources.values()}
    assert kinds == {"decision_record"}


@pytest.mark.asyncio
async def test_dict_without_list_key_treated_as_single_source():
    """If the tool returns a dict that has no `results`/`items`/`data`
    list-valued key, treat the whole dict as one source to annotate.
    """

    @cite_tool_output(kind="wiki_page")
    async def tool(channel_id: str) -> dict:
        return {
            "channel_id": channel_id,
            "page_type": "faq",
            "text": "faq excerpt",
        }

    r, tok = bind()
    try:
        out = await tool(channel_id="C1")
    finally:
        reset(tok)

    assert r.registered_count == 1
    assert "_cite" in out


@pytest.mark.asyncio
async def test_empty_results_list_does_not_register():
    @cite_tool_output(kind="web_result")
    async def tool() -> dict:
        return {"answer": "none", "results": []}

    r, tok = bind()
    try:
        out = await tool()
    finally:
        reset(tok)

    # Empty results list is NOT unwrapped as citable items; it's treated
    # as a single-source dict, which has no `text` so registration is skipped.
    assert r.registered_count == 0
    assert "_cite" not in out


@pytest.mark.asyncio
async def test_flag_off_is_noop():
    """No registry bound (flag off) → decorator is a pass-through."""

    @cite_tool_output(kind="wiki_page")
    async def tool(channel_id: str) -> dict:
        return {"channel_id": channel_id, "page_type": "overview", "text": "x"}

    # No bind() — no current registry.
    out = await tool(channel_id="C1")
    assert "_cite" not in out
