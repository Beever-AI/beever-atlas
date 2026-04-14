"""Fallback coverage guaranteeing NO wiki page kind ever ships empty.

Mirrors the pattern in ``test_faq_glossary_fallback.py`` — mocks
``_call_llm`` to return empty content (or raise), then asserts the compiled
page still has non-empty deterministic content.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.models.domain import AtomicFact, ChannelSummary, TopicCluster
from beever_atlas.wiki.compiler import WikiCompiler
from beever_atlas.wiki.schemas import CompiledPageContent


def _make_compiler() -> WikiCompiler:
    provider = MagicMock()
    provider.get_model_string.return_value = "gemini-2.5-flash"
    with patch("beever_atlas.wiki.compiler.get_llm_provider", return_value=provider):
        return WikiCompiler()


def _make_fact(
    *,
    text: str = "A memory about the project.",
    author: str = "Alice",
    ts: str = "1700000000.000000",
) -> AtomicFact:
    return AtomicFact(
        channel_id="C0TEST",
        memory_text=text,
        author_name=author,
        message_ts=ts,
        fact_type="observation",
        importance="medium",
        quality_score=0.8,
    )


def _gathered_base() -> dict:
    cluster = TopicCluster(
        id="c1",
        channel_id="C0TEST",
        title="Python Backend",
        summary="Python backend services.",
        topic_tags=["python", "backend"],
        member_ids=["f1"],
        member_count=3,
        key_facts=[{"fact": "Team uses Python for backend services."}],
        date_range_end="2024-01-15",
    )
    channel_summary = ChannelSummary(
        id="s1",
        channel_id="C0TEST",
        channel_name="#test",
        text="Test channel text.",
        themes="Python themes.",
        description="Python backend work.",
        top_people=[
            {"name": "Alice", "role": "lead", "topic_count": 5},
            {"name": "Bob", "role": "contributor", "topic_count": 3},
        ],
        recent_activity_summary={"highlights": [{"text": "Shipped v1.0"}]},
    )
    persons = [
        {
            "entity": SimpleNamespace(name="Alice"),
            "decided": ["adopt Python"],
            "works_on": ["backend"],
            "uses": ["FastAPI"],
        },
        {
            "entity": SimpleNamespace(name="Bob"),
            "decided": [],
            "works_on": ["frontend"],
            "uses": [],
        },
    ]
    return {
        "channel_id": "C0TEST",
        "channel_name": "#test",
        "channel_summary": channel_summary,
        "clusters": [cluster],
        "cluster_facts": {"c1": []},
        "recent_facts": [
            _make_fact(text="First recent event", ts="1700000100.0"),
            _make_fact(text="Second recent event", author="Bob", ts="1700000200.0"),
        ],
        "media_facts": [],
        "decisions": [],
        "technologies": [],
        "projects": [],
        "persons": persons,
        "total_facts": 3,
    }


@pytest.mark.asyncio
async def test_overview_fallback_when_llm_empty() -> None:
    compiler = _make_compiler()
    gathered = _gathered_base()
    with patch.object(
        compiler,
        "_call_llm",
        new=AsyncMock(return_value=CompiledPageContent(content="", summary="")),
    ):
        page = await compiler._compile_overview(gathered)
    assert page.content.strip()
    assert "Python Backend" in page.content


@pytest.mark.asyncio
async def test_people_fallback_when_llm_empty() -> None:
    compiler = _make_compiler()
    gathered = _gathered_base()
    with patch.object(
        compiler,
        "_call_llm",
        new=AsyncMock(return_value=CompiledPageContent(content="", summary="")),
    ):
        page = await compiler._compile_people(gathered)
    assert page.content.strip()
    assert "Alice" in page.content
    assert "Bob" in page.content


@pytest.mark.asyncio
async def test_activity_fallback_when_llm_empty() -> None:
    compiler = _make_compiler()
    gathered = _gathered_base()
    with patch.object(
        compiler,
        "_call_llm",
        new=AsyncMock(return_value=CompiledPageContent(content="", summary="")),
    ):
        page = await compiler._compile_activity(gathered)
    assert page.content.strip()
    # At least one recent-fact text should surface.
    assert "First recent event" in page.content or "Second recent event" in page.content


@pytest.mark.asyncio
async def test_resources_fallback_when_llm_empty() -> None:
    compiler = _make_compiler()
    gathered = _gathered_base()
    # No media_facts -> _assemble_resources_markdown returns "" -> fallback fires.
    page = await compiler._compile_resources(gathered)
    assert page.content.strip()
    assert "Resources & Media" in page.content


@pytest.mark.asyncio
async def test_subtopic_fallback_when_llm_empty() -> None:
    compiler = _make_compiler()
    sub_facts = [
        _make_fact(text="Sub-fact A about FastAPI routing"),
        _make_fact(text="Sub-fact B about auth middleware", author="Bob"),
    ]
    sub_info = {"title": "Auth Middleware", "fact_indices": [0, 1], "summary": ""}
    with patch.object(
        compiler,
        "_call_llm",
        new=AsyncMock(return_value=CompiledPageContent(content="", summary="")),
    ):
        page = await compiler._compile_subtopic_page(
            parent_slug="python-backend",
            parent_title="Python Backend",
            sub_info=sub_info,
            all_sorted_facts=sub_facts,
        )
    assert page.content.strip()
    assert "Auth Middleware" in page.content
    # At least one sub-fact text should appear.
    assert "Sub-fact A" in page.content or "Sub-fact B" in page.content


@pytest.mark.asyncio
async def test_overview_fallback_when_llm_raises() -> None:
    compiler = _make_compiler()
    gathered = _gathered_base()
    with patch.object(
        compiler,
        "_call_llm",
        new=AsyncMock(side_effect=RuntimeError("LLM blew up")),
    ):
        # Must not propagate — page is built via fallback.
        page = await compiler._compile_overview(gathered)
    assert page.content.strip()
    assert "Python Backend" in page.content
