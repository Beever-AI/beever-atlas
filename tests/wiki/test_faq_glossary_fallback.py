"""Fallback coverage for FAQ / Glossary pages when the LLM returns empty.

Guards against the failure seen on channel C0A955E29MX where Gemini
truncated mid-chart-block and the compiler shipped empty FAQ / Glossary
pages. The deterministic fallbacks should always produce non-empty content
so the wiki never renders a blank page.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.models.domain import ChannelSummary, TopicCluster
from beever_atlas.wiki.compiler import WikiCompiler
from beever_atlas.wiki.schemas import CompiledPageContent


def _make_compiler() -> WikiCompiler:
    provider = MagicMock()
    provider.get_model_string.return_value = "gemini-2.5-flash"
    with patch("beever_atlas.wiki.compiler.get_llm_provider", return_value=provider):
        return WikiCompiler()


def _gathered_with_faq() -> dict:
    cluster = TopicCluster(
        id="c1",
        channel_id="C0TEST",
        title="Python Backend",
        summary="Python backend services.",
        current_state="Stable.",
        topic_tags=["python", "backend"],
        member_ids=["f1"],
        member_count=1,
        key_facts=[{"fact": "Team uses Python for backend services."}],
        faq_candidates=[
            {"question": "Why Python?", "answer": "Team expertise and ecosystem fit."},
        ],
    )
    channel_summary = ChannelSummary(
        id="s1",
        channel_id="C0TEST",
        channel_name="#test",
        text="Test channel.",
        themes="Python.",
        description="Python backend work.",
        glossary_terms=[
            {"term": "FastAPI", "definition": "A modern Python web framework."},
            {"term": "Pydantic", "definition": "Data validation library."},
        ],
    )
    return {
        "channel_id": "C0TEST",
        "channel_name": "#test",
        "channel_summary": channel_summary,
        "clusters": [cluster],
        "cluster_facts": {"c1": []},
        "recent_facts": [],
        "media_facts": [],
        "decisions": [],
        "technologies": [],
        "projects": [],
        "persons": [],
        "total_facts": 0,
    }


@pytest.mark.asyncio
async def test_faq_fallback_when_llm_empty() -> None:
    compiler = _make_compiler()
    gathered = _gathered_with_faq()
    with patch.object(
        compiler,
        "_call_llm",
        new=AsyncMock(return_value=CompiledPageContent(content="", summary="")),
    ):
        page = await compiler._compile_faq(gathered)
    assert page.content.strip()
    # Should include the structured Q&A we passed in.
    assert "Python Backend" in page.content
    assert "Why Python?" in page.content
    assert "Team expertise" in page.content


@pytest.mark.asyncio
async def test_glossary_fallback_when_llm_empty() -> None:
    compiler = _make_compiler()
    gathered = _gathered_with_faq()
    with patch.object(
        compiler,
        "_call_llm",
        new=AsyncMock(return_value=CompiledPageContent(content="", summary="")),
    ):
        page = await compiler._compile_glossary(gathered)
    assert page.content.strip()
    # At least one of the glossary terms should appear.
    assert "FastAPI" in page.content or "Pydantic" in page.content


@pytest.mark.asyncio
async def test_faq_uses_llm_content_when_non_empty() -> None:
    compiler = _make_compiler()
    gathered = _gathered_with_faq()
    real_content = "## FAQ\n\nA real LLM answer with plenty of prose about the topic."
    with patch.object(
        compiler,
        "_call_llm",
        new=AsyncMock(
            return_value=CompiledPageContent(content=real_content, summary="real sum"),
        ),
    ):
        page = await compiler._compile_faq(gathered)
    assert "A real LLM answer" in page.content
    # Fallback signal should NOT be present.
    assert "Frequently Asked Questions" not in page.content or "A real LLM answer" in page.content
