"""Regression: _compile_overview must not IndexError on an empty channel.

When a channel has no media facts and no glossary terms, ``summary.glossary_terms``
may be ``None`` (legacy docs) or ``[]`` and ``media_facts`` may be empty. The
overview compiler should degrade gracefully to an empty-but-valid WikiPage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from beever_atlas.models.domain import ChannelSummary
from beever_atlas.wiki.compiler import CompiledPageContent, WikiCompiler


def _empty_gathered() -> dict:
    summary = ChannelSummary(channel_id="C1", channel_name="empty-channel")
    # Exercise the defensive path: glossary_terms explicitly None.
    summary.glossary_terms = None  # type: ignore[assignment]
    return {
        "channel_summary": summary,
        "clusters": [],
        "media_facts": [],
        "recent_facts": [],
        "technologies": [],
        "projects": [],
        "decisions": [],
        "persons": [],
        "total_facts": 0,
        "cluster_facts": {},
    }


@pytest.mark.asyncio
async def test_compile_overview_empty_channel_no_index_error():
    compiler = WikiCompiler.__new__(WikiCompiler)
    # Inject only the attributes _compile_overview touches.
    compiler._lang = "en"  # type: ignore[attr-defined]

    empty_content = CompiledPageContent(content="(no content)", summary="")

    with (
        patch.object(WikiCompiler, "_fmt_prompt", return_value="prompt"),
        patch.object(WikiCompiler, "_call_llm", new_callable=AsyncMock, return_value=empty_content),
        patch.object(WikiCompiler, "_page_title", return_value="Overview"),
        patch.object(WikiCompiler, "_postprocess_content", staticmethod(lambda s: s)),
    ):
        page = await compiler._compile_overview(_empty_gathered())

    assert page.id == "overview"
    assert page.slug == "overview"
    assert page.memory_count == 0
    assert page.citations == []
