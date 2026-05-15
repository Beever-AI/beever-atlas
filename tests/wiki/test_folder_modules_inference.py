"""Tests for ``_compile_folder_page`` module-to-fact_type promotion (F2).

The folder dashboard's predicates (``cross_cutting_decisions``,
``quote_highlights`` rollups, etc.) need ``fact_type`` on each
descendant's facts to fire. Citation-only children carry no
``fact_type``, so the compiler promotes structured items from each
child's persisted ``modules`` array. Originally only ``decision_log``
was promoted; the redesign extends to ``quote_highlights``,
``tension_callout``, and ``open_questions`` so folders made of
non-decision pages also surface useful content.

These tests intercept ``compile_folder_page_modular`` and assert the
``descendants`` argument the compiler hands it carries the promoted
fact entries with the right ``fact_type``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from beever_atlas.models.domain import WikiCitation, WikiPage
from beever_atlas.wiki.compiler import WikiCompiler
from beever_atlas.wiki.modules.orchestrator import ModularPageOutput


def _page(
    *,
    page_id: str = "topic-x",
    slug: str = "topic-x",
    title: str = "Topic X",
    citations: list[WikiCitation] | None = None,
    modules: list[dict[str, Any]] | None = None,
) -> WikiPage:
    return WikiPage(
        id=page_id,
        slug=slug,
        title=title,
        page_type="topic",
        section_number="1",
        content="",
        summary="",
        memory_count=0,
        last_updated=datetime.now(tz=UTC),
        citations=citations or [],
        modules=modules or [],
    )


async def _run_and_capture_descendants(children_pages: list[WikiPage]) -> list[dict]:
    """Invoke ``_compile_folder_page`` with a stub LLM-success path and
    return the ``descendants`` arg the orchestrator received."""
    captured: dict[str, Any] = {}

    async def _fake_modular(*args, **kwargs) -> ModularPageOutput:
        captured["descendants"] = kwargs.get("descendants") or (args[2] if len(args) > 2 else [])
        # Return a successful (non-fallback) output so the compiler
        # uses the modular result and we don't fall through to the
        # legacy prompt path.
        return ModularPageOutput(
            content="",
            summary="",
            modules=[],
            planner_module_count=0,
            rendered_module_count=0,
            fell_back=False,
        )

    compiler = WikiCompiler.__new__(WikiCompiler)
    compiler._llm_generate_json = AsyncMock(return_value="{}")  # type: ignore[attr-defined]

    with patch(
        "beever_atlas.wiki.modules.orchestrator.compile_folder_page_modular",
        side_effect=_fake_modular,
    ):
        await compiler._compile_folder_page(
            folder_slug="x",
            folder_title="X",
            children_pages=children_pages,
        )
    return captured.get("descendants") or []


@pytest.mark.asyncio
async def test_promotes_quote_highlights_from_child_modules() -> None:
    """A child carrying a ``quote_highlights`` module should surface
    ``fact_type='quote'`` entries in the folder's descendant aggregate."""
    child = _page(
        modules=[
            {
                "id": "quote_highlights",
                "data": {
                    "quotes": [
                        {
                            "text": "We must ship Tuesday.",
                            "author": "Alan",
                            "importance": "high",
                            "fact_id": "f1",
                        },
                        {
                            "memory_text": "Cache invalidation is the hard part.",
                            "made_by": "Bob",
                            "date": "2026-04-01",
                        },
                    ],
                },
            }
        ],
    )
    descendants = await _run_and_capture_descendants([child])
    assert len(descendants) == 1
    quote_facts = [f for f in descendants[0]["facts"] if f.get("fact_type") == "quote"]
    assert len(quote_facts) == 2
    assert quote_facts[0]["memory_text"] == "We must ship Tuesday."
    assert quote_facts[0]["author_name"] == "Alan"
    assert quote_facts[0]["fact_id"] == "f1"
    assert quote_facts[1]["memory_text"] == "Cache invalidation is the hard part."
    assert quote_facts[1]["author_name"] == "Bob"


@pytest.mark.asyncio
async def test_promotes_tension_callout_from_child_modules() -> None:
    """A ``tension_callout`` module should surface a single
    ``fact_type='tension'`` entry per child."""
    child = _page(
        modules=[
            {
                "id": "tension_callout",
                "data": {
                    "title": "REST vs GraphQL for the new endpoint",
                    "since": "2026-03-12",
                    "positions": [
                        {"author": "Alan", "fact_id": "t1"},
                        {"author": "Bob", "fact_id": "t2"},
                    ],
                },
            }
        ],
    )
    descendants = await _run_and_capture_descendants([child])
    tension_facts = [f for f in descendants[0]["facts"] if f.get("fact_type") == "tension"]
    assert len(tension_facts) == 1
    assert tension_facts[0]["memory_text"] == "REST vs GraphQL for the new endpoint"
    assert tension_facts[0]["author_name"] == "Alan"
    assert tension_facts[0]["fact_id"] == "t1"
    assert tension_facts[0]["importance"] == "high"


@pytest.mark.asyncio
async def test_promotes_open_questions_from_child_modules() -> None:
    """An ``open_questions`` module should surface
    ``fact_type='open_question'`` entries."""
    child = _page(
        modules=[
            {
                "id": "open_questions",
                "data": {
                    "questions": [
                        {
                            "question": "Do we need rate-limiting on the push endpoint?",
                            "raised_by": "Alan",
                            "raised": "2026-04-15",
                        },
                        {
                            "text": "What's our SLA target?",
                            "author": "Bob",
                        },
                    ],
                },
            }
        ],
    )
    descendants = await _run_and_capture_descendants([child])
    oq_facts = [f for f in descendants[0]["facts"] if f.get("fact_type") == "open_question"]
    assert len(oq_facts) == 2
    assert oq_facts[0]["memory_text"] == "Do we need rate-limiting on the push endpoint?"
    assert oq_facts[0]["author_name"] == "Alan"
    assert oq_facts[1]["memory_text"] == "What's our SLA target?"
    assert oq_facts[1]["author_name"] == "Bob"


@pytest.mark.asyncio
async def test_decision_log_promotion_still_works() -> None:
    """Regression check — the pre-existing ``decision_log`` promotion
    must keep emitting ``fact_type='decision'`` entries."""
    child = _page(
        modules=[
            {
                "id": "decision_log",
                "data": {
                    "decisions": [
                        {
                            "decision": "Ship feature flag default OFF",
                            "made_by": "Alan",
                            "fact_id": "d1",
                            "importance": "high",
                            "date": "2026-04-20",
                        }
                    ],
                },
            }
        ],
    )
    descendants = await _run_and_capture_descendants([child])
    decisions = [f for f in descendants[0]["facts"] if f.get("fact_type") == "decision"]
    assert len(decisions) == 1
    assert decisions[0]["memory_text"] == "Ship feature flag default OFF"
    assert decisions[0]["fact_id"] == "d1"


@pytest.mark.asyncio
async def test_promotion_handles_malformed_module() -> None:
    """A module dict missing or with malformed inner data must not
    crash the compiler — emit zero promoted entries instead."""
    # ``WikiPage.modules`` is Pydantic-typed ``list[dict]`` so the
    # production ``isinstance(mod, dict)`` guard is unreachable from
    # normal data flow — Pydantic rejects non-dict entries at model
    # construction. The defensive cases below cover the shapes that
    # CAN reach the compiler: known module ids with malformed inner
    # data, and unknown module ids the dispatch loop ignores.
    malformed_modules: list[dict[str, Any]] = [
        # Right id, wrong inner shape.
        {"id": "quote_highlights", "data": {"quotes": "not a list"}},
        # Right id, list of wrong types.
        {"id": "open_questions", "data": {"questions": [None, "string", 42]}},
        # Tension with empty title — only emits when ``title`` is non-empty.
        {"id": "tension_callout", "data": {"title": "   ", "positions": []}},
        # Unknown module id — ignored entirely.
        {"id": "unknown_module", "data": {"foo": "bar"}},
    ]
    child = _page(modules=malformed_modules)
    descendants = await _run_and_capture_descendants([child])
    promoted = [
        f
        for f in descendants[0]["facts"]
        if f.get("fact_type") in {"quote", "tension", "open_question", "decision"}
    ]
    assert promoted == []
