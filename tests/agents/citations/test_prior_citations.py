"""Unit tests for transitive qa_history citations (Phase 3)."""

from __future__ import annotations

import pytest

from beever_atlas.agents.citations.registry import bind, reset
from beever_atlas.agents.tools._citation_decorator import (
    _extract_prior_citations,
    cite_tool_output,
)


def test_extract_prior_citations_from_envelope():
    past_entry = {
        "text": "Answer summary",
        "citations": {
            "items": [],
            "sources": [
                {
                    "id": "src_a",
                    "kind": "channel_message",
                    "title": "alice · #design",
                    "native": {
                        "author": "alice",
                        "channel_name": "design",
                        "timestamp": "2026-04-01",
                    },
                },
                {
                    "id": "src_b",
                    "kind": "web_result",
                    "title": "Example site",
                    "native": {"author": None, "channel_name": None},
                },
            ],
            "refs": [],
        },
    }
    out = _extract_prior_citations(past_entry)
    assert len(out) == 2
    assert out[0]["id"] == "src_a"
    assert out[0]["author"] == "alice"
    assert out[0]["channel"] == "design"
    assert out[1]["kind"] == "web_result"


def test_extract_prior_citations_from_legacy_list():
    past_entry = {
        "citations": [
            {
                "type": "channel_fact",
                "author": "bob",
                "channel": "general",
                "timestamp": "2026-01-01",
                "text": "some fact",
            },
        ]
    }
    out = _extract_prior_citations(past_entry)
    assert len(out) == 1
    assert out[0]["author"] == "bob"
    assert out[0]["channel"] == "general"
    assert out[0]["id"] is None  # legacy entries have no structured id


def test_extract_prior_citations_empty():
    assert _extract_prior_citations({}) == []
    assert _extract_prior_citations({"citations": None}) == []
    assert _extract_prior_citations({"citations": []}) == []


def test_extract_prior_citations_truncates_to_five():
    past_entry = {
        "citations": {
            "items": [],
            "sources": [
                {"id": f"src_{i}", "kind": "channel_message", "native": {}} for i in range(10)
            ],
            "refs": [],
        }
    }
    out = _extract_prior_citations(past_entry)
    assert len(out) == 5


@pytest.mark.asyncio
async def test_qa_history_registration_carries_prior_citations():
    """End-to-end: search_qa_history tool → registered source carries
    native.prior_citations trimmed from the past answer's envelope."""

    @cite_tool_output(kind="qa_history")
    async def fake_search() -> list[dict]:
        return [
            {
                "question": "What is X?",
                "answer": "It is Y.",
                "text": "It is Y.",
                "qa_id": "qa-1",
                "session_id": "sess-1",
                "timestamp": "2026-04-10",
                "citations": {
                    "items": [],
                    "sources": [
                        {
                            "id": "src_parent",
                            "kind": "channel_message",
                            "title": "root",
                            "native": {
                                "author": "root-author",
                                "channel_name": "root-channel",
                                "timestamp": "2026-04-09",
                            },
                        }
                    ],
                    "refs": [],
                },
            }
        ]

    r, tok = bind()
    try:
        await fake_search()
    finally:
        reset(tok)

    assert r.registered_count == 1
    src = list(r._sources.values())[0]
    assert src.kind == "qa_history"
    priors = src.native.get("prior_citations")
    assert isinstance(priors, list)
    assert len(priors) == 1
    assert priors[0]["id"] == "src_parent"
    assert priors[0]["author"] == "root-author"
