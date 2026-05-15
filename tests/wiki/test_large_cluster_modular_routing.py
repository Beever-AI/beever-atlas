"""Round 4 regression — ≥15-fact clusters MUST route through the
modular pipeline so the v2 cards win on every topic page regardless
of size.

Pre-fix: ``_compile_topic_page`` called the modular path only for
clusters strictly under ``TOPIC_SUBPAGE_THRESHOLD`` (15). Larger
clusters split into sub-pages via ``_analyze_topic`` and rendered the
PARENT overview through the legacy ``TOPIC_PROMPT_V2`` flow — which
shipped a parent ``WikiPage`` with empty ``modules`` so the frontend
fell back to ``WikiMarkdown(page.content)`` and the v1 GFM table is
what the user saw.

Post-fix: ``_compile_topic_page`` routes the parent through
``compile_topic_page_modular`` regardless of fact count. The
sub-page split logic still runs first (via ``_analyze_topic`` +
``_compile_subtopic_page``); the produced sub-pages flow into the
parent's ``subpage_cards`` module via ``signals.child_count``.

What gets asserted:
  - The orchestrator output has a NON-EMPTY ``modules`` list.
    ``TopicPage.tsx`` only switches to ``ModuleRenderer`` when
    ``page.modules.length > 0``; an empty list means the legacy
    table wins.
  - ``key_facts`` is present with ``renderer_kind == "frontend"``
    AND a non-empty ``items`` list. That is the v2-cards contract;
    a python-renderer ``key_facts`` (the legacy fallback shape)
    means the WikiTab still shows a GFM table even though the page
    technically has modules.
  - ``subpage_cards`` appears when ≥ 2 sub-pages exist (singleton
    parents are suppressed post-validation by design).

Note: ``page.content`` STILL carries the legacy GFM key-facts table
as a markdown-render fallback for older readers (search index,
copy-out) — that's intentional persistence, NOT a regression. The
frontend ignores it whenever ``page.modules`` is populated.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from beever_atlas.wiki.modules.orchestrator import (
    ModularPageOutput,
    compile_topic_page_modular,
)
from beever_atlas.wiki.modules.planner import compute_signals


def _make_facts(n: int) -> list[dict[str, Any]]:
    """Build ``n`` realistic facts with mixed types so eligibility
    predicates fire (5+ facts → key_facts; some decisions → decision
    archetype potentially)."""
    out: list[dict[str, Any]] = []
    for i in range(n):
        out.append(
            {
                "memory_text": (
                    f"Fact #{i}: a substantive observation about the topic "
                    f"that needs at least 80 chars so the title truncation "
                    f"gets exercised in the renderer."
                ),
                "fact_type": "claim" if i % 4 else "decision",
                "author_name": f"User{i % 4}",
                "date": f"2026-04-{(i % 28) + 1:02d}",
                "importance": 9 if i % 5 == 0 else 6,
                "quality_score": 0.9,
            }
        )
    return out


def _make_subpages(count: int = 3) -> list[dict[str, Any]]:
    """Mimic the shape ``_analyze_topic`` outputs flow into the
    orchestrator as. ``compile_topic_page_modular`` consumes children
    via ``render_inputs["children"]`` (built from sub-page WikiPage
    objects in the compiler adapter)."""
    return [
        {
            "title": f"Sub-topic {i + 1}",
            "slug": f"parent--sub-topic-{i + 1}",
            "summary": f"Brief summary for sub-topic {i + 1}.",
        }
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_22_fact_cluster_renders_modular_not_legacy_gfm_table() -> None:
    """22-fact cluster (the user's Landing Page case) must produce a
    modular output with frontend-renderer ``key_facts``. Pre-fix the
    parent rendered through ``TOPIC_PROMPT_V2`` and shipped with empty
    ``modules`` — so the WikiTab fell back to ``WikiMarkdown`` over
    ``page.content`` and showed the v1 GFM table.
    """
    facts = _make_facts(22)
    sub_children = _make_subpages(3)

    async def stub_llm(prompt: str) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "summary"},
                        {"id": "key_facts", "anchor": "key-facts"},
                        {"id": "subpage_cards", "anchor": "sub-pages"},
                        {"id": "provenance_drawer", "anchor": "sources"},
                    ]
                },
                "tldr": "**Landing Page topic with 22 memories.**",
                "overview": "Wide-ranging discussion of the landing page.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:subpage_cards>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    cluster_arg = {
        "title": "Beever Atlas Landing Page",
        "member_facts": facts,
        "child_count": len(sub_children),
    }
    signals = compute_signals(cluster=cluster_arg)
    render_inputs = {
        "facts": facts,
        "decisions": [],
        "open_questions": [],
        "children": sub_children,
    }

    out = await compile_topic_page_modular(
        title="Beever Atlas Landing Page",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=facts[:8],
        top_people=[{"name": "User0"}],
        date_range_start="2026-04-01",
        date_range_end="2026-04-28",
        llm=stub_llm,
    )

    # Modular path produced output (not catastrophic fallback).
    assert isinstance(out, ModularPageOutput)
    assert out.fell_back is False, (
        "modular path must succeed for 22-fact cluster; "
        f"got fell_back=True with modules={[m['id'] for m in out.modules]}"
    )
    assert out.modules, "expected non-empty modules list"

    # ``key_facts`` is present and renders as a frontend module
    # (severity-grouped cards), not the legacy markdown table.
    by_id = {m["id"]: m for m in out.modules}
    assert "key_facts" in by_id, "key_facts MUST fire on a 22-fact page"
    kf_data = by_id["key_facts"]["data"]
    assert kf_data["renderer_kind"] == "frontend", (
        "key_facts must be frontend renderer; got "
        f"{kf_data['renderer_kind']!r}. Did the route fall back to legacy?"
    )
    assert "items" in kf_data
    assert len(kf_data["items"]) >= 5

    # ``subpage_cards`` is present (3 children passed in — above the
    # singleton-suppression threshold of 1).
    assert "subpage_cards" in by_id, "subpage_cards MUST fire when child_count >= 2"

    # ``modules`` is non-empty — this is the contract ``TopicPage.tsx``
    # checks (``page.modules.length > 0``) before mounting the v2
    # ``ModuleRenderer``. Empty modules → frontend falls back to
    # ``WikiMarkdown(page.content)`` and the v1 GFM table is what the
    # user sees.
    assert len(out.modules) >= 3, (
        "modular path must produce ≥3 modules; "
        f"empty plan would route the frontend to WikiMarkdown over "
        f"page.content. modules={[m['id'] for m in out.modules]}"
    )


@pytest.mark.asyncio
async def test_subpage_cards_module_listed_when_children_present() -> None:
    """When ``child_count >= 1`` and sub-pages flow through
    ``render_inputs['children']``, the parent plan's
    ``subpage_cards`` module renders the children list (no markdown
    table fallback)."""
    facts = _make_facts(20)
    sub_children = _make_subpages(2)

    async def stub_llm(prompt: str) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "summary"},
                        {"id": "key_facts", "anchor": "key-facts"},
                        {"id": "subpage_cards", "anchor": "sub-pages"},
                        {"id": "provenance_drawer", "anchor": "sources"},
                    ]
                },
                "tldr": "**Parent topic.**",
                "overview": "Parent overview.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:subpage_cards>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    cluster_arg = {
        "title": "Parent topic",
        "member_facts": facts,
        "child_count": len(sub_children),
    }
    signals = compute_signals(cluster=cluster_arg)
    render_inputs = {
        "facts": facts,
        "decisions": [],
        "open_questions": [],
        "children": sub_children,
    }

    out = await compile_topic_page_modular(
        title="Parent topic",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=facts[:8],
        top_people=[{"name": "User0"}],
        llm=stub_llm,
    )

    by_id = {m["id"]: m for m in out.modules}
    assert "subpage_cards" in by_id

    # The subpage_cards module's deterministic renderer produces a
    # GFM list — verify the children titles appear (so the Substitution
    # actually wired up the data, not an empty marker drop).
    assert "Sub-topic 1" in out.content
    assert "Sub-topic 2" in out.content


@pytest.mark.asyncio
async def test_singleton_subpage_cards_is_suppressed() -> None:
    """Suppression rule: ``subpage_cards`` with ``child_count == 1``
    is dropped post-plan because a single child reads better as an
    inline link. The predicate (≥1) lets it through validation; the
    suppression pass strips it."""
    facts = _make_facts(20)
    sub_children = _make_subpages(1)

    async def stub_llm(prompt: str) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "summary"},
                        {"id": "key_facts", "anchor": "key-facts"},
                        {"id": "subpage_cards", "anchor": "sub-pages"},
                        {"id": "provenance_drawer", "anchor": "sources"},
                    ]
                },
                "tldr": "**Singleton.**",
                "overview": "Overview.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:subpage_cards>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    cluster_arg = {
        "title": "Singleton",
        "member_facts": facts,
        "child_count": len(sub_children),
    }
    signals = compute_signals(cluster=cluster_arg)
    render_inputs = {
        "facts": facts,
        "decisions": [],
        "open_questions": [],
        "children": sub_children,
    }

    out = await compile_topic_page_modular(
        title="Singleton",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=facts[:8],
        top_people=[],
        llm=stub_llm,
    )

    by_id = {m["id"]: m for m in out.modules}
    assert "subpage_cards" not in by_id, "child_count == 1 should suppress subpage_cards"


@pytest.mark.asyncio
async def test_try_compile_topic_modular_threads_subpages_through_signals() -> None:
    """Compiler-integration check — when ``_try_compile_topic_modular``
    is called with ``sub_pages=[...]``, the orchestrator's signals
    receive ``child_count = len(sub_pages)`` and the parent's
    ``subpage_cards`` module fires.

    Pre-fix the modular adapter passed ``child_count=0`` regardless of
    the cluster's split state, so the planner never picked
    ``subpage_cards`` even when the parent had children.
    """
    from unittest.mock import MagicMock, patch

    from beever_atlas.models.domain import WikiPage
    from beever_atlas.wiki.compiler import WikiCompiler

    provider = MagicMock()
    provider.get_model_string.return_value = "gemini-2.5-flash"
    with patch("beever_atlas.wiki.compiler.get_llm_provider", return_value=provider):
        compiler = WikiCompiler()

    # Stub the LLM to return a plan that includes ``subpage_cards``.
    # The planner's HARD RULE for ``child_count >= 1`` instructs the
    # LLM to include it; we mirror that here.
    async def _stub_llm(self_inner, prompt: str, **kwargs) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "summary"},
                        {"id": "key_facts", "anchor": "kf"},
                        {"id": "subpage_cards", "anchor": "sub-pages"},
                        {"id": "provenance_drawer", "anchor": "src"},
                    ]
                },
                "tldr": "**Parent.**",
                "overview": "Overview prose.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:subpage_cards>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    # Build a minimum cluster + gathered shape that ``_try_compile_topic_modular``
    # consumes. Fields not consumed by the modular adapter are left
    # off to keep the fixture surface tight.
    class _StubCluster:
        id = "cluster-large"
        title = "Large parent topic"
        summary = ""
        decisions: list[dict] = []
        key_entities: list[dict] = []
        key_relationships: list[dict] = []
        topic_tags: list[str] = []
        people: list[dict] = []
        technologies: list[dict] = []
        projects: list[dict] = []
        member_count = 22
        date_range_start = ""
        date_range_end = ""
        related_cluster_ids: list[str] = []
        open_questions = ""

    # Use real ``AtomicFact`` instances — they cover the full set of
    # attribute names the compiler reads on the citation/permalink
    # path so the test stays insulated from drift in those internals.
    from beever_atlas.models.domain import AtomicFact

    sorted_facts = [
        AtomicFact(
            id=f"fact-{i}",
            memory_text=(
                f"Fact #{i}: a substantive observation about the topic "
                f"that needs at least 80 chars so the truncation gets "
                f"exercised."
            ),
            quality_score=0.9,
            channel_id="C123",
            platform="",
            author_name=f"User{i % 4}",
            message_ts=f"2026-04-{(i % 28) + 1:02d}T00:00:00",
            fact_type="claim" if i % 4 else "decision",
            importance=str(9 if i % 5 == 0 else 6),
        )
        for i in range(22)
    ]
    gathered = {
        "cluster_facts": {_StubCluster.id: sorted_facts},
        "clusters": [_StubCluster()],
        "channel_summary": MagicMock(glossary_terms=[]),
    }

    # Sub-pages built by an upstream call to ``_compile_subtopic_page``.
    # Three children — above the singleton-suppression threshold.
    sub_pages: list[WikiPage] = [
        WikiPage(
            id=f"topic-large-parent--sub-{i}",
            slug=f"large-parent--sub-{i}",
            title=f"Sub-page {i}",
            page_type="sub-topic",
            parent_id="topic-large-parent",
            content="content body that is long enough" * 5,
            summary=f"Summary {i}",
            memory_count=5,
        )
        for i in range(3)
    ]

    with patch.object(WikiCompiler, "_llm_generate_json", _stub_llm):
        page = await compiler._try_compile_topic_modular(
            _StubCluster(), gathered, sorted_facts, sub_pages=sub_pages
        )

    assert page is not None, "modular adapter must produce a parent page"
    assert page.modules, "parent must have a non-empty modules list"
    by_id = {m["id"]: m for m in page.modules}
    assert "subpage_cards" in by_id, (
        "subpage_cards module must fire on a 22-fact parent with 3 sub-pages"
    )
    # ``key_facts`` stays frontend-renderer-kind on this large parent.
    assert by_id["key_facts"]["data"]["renderer_kind"] == "frontend"
    # The parent's ``children`` field is populated so channel-tree
    # builders can walk parent → children.
    assert len(page.children) == 3
    assert {ch.title for ch in page.children} == {
        "Sub-page 0",
        "Sub-page 1",
        "Sub-page 2",
    }


@pytest.mark.asyncio
async def test_large_cluster_without_children_skips_subpage_cards() -> None:
    """When ``_analyze_topic`` decides a 22-fact cluster does NOT
    need splitting (cohesive single-theme cluster), the modular
    parent runs WITHOUT children — ``subpage_cards`` predicate
    fails (``child_count == 0``) and is rejected by the validator.
    """
    facts = _make_facts(22)

    async def stub_llm(prompt: str) -> str:
        # The LLM tries to include subpage_cards anyway (typical drift);
        # the validator drops it because the predicate fails on
        # ``child_count == 0``.
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "summary"},
                        {"id": "key_facts", "anchor": "kf"},
                        {"id": "subpage_cards", "anchor": "sub"},  # rejected
                        {"id": "provenance_drawer", "anchor": "src"},
                    ]
                },
                "tldr": "**Cohesive 22-fact topic.**",
                "overview": "Single-theme cluster.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    cluster_arg = {
        "title": "Cohesive Topic",
        "member_facts": facts,
        "child_count": 0,
    }
    signals = compute_signals(cluster=cluster_arg)
    render_inputs = {
        "facts": facts,
        "decisions": [],
        "open_questions": [],
        "children": [],
    }

    out = await compile_topic_page_modular(
        title="Cohesive Topic",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=facts[:8],
        top_people=[],
        llm=stub_llm,
    )

    by_id = {m["id"]: m for m in out.modules}
    assert "subpage_cards" not in by_id, (
        "subpage_cards must be dropped by the validator when "
        "child_count == 0 even if the LLM tries to include it"
    )
    # The page still renders other modules normally.
    assert "key_facts" in by_id


@pytest.mark.parametrize("child_count", [2, 3, 5, 8])
@pytest.mark.asyncio
async def test_subpage_cards_fires_for_multiple_child_counts(child_count: int) -> None:
    """Verify the predicate (>=1) + suppression (=1) combination
    leaves exactly the right window: 2+ children fire ``subpage_cards``,
    singleton (=1) is suppressed, and 0 fails the predicate. This
    parametrise covers the ``>=2`` window to pin behaviour."""
    facts = _make_facts(20)
    sub_children = _make_subpages(child_count)

    async def stub_llm(prompt: str) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "s"},
                        {"id": "key_facts", "anchor": "kf"},
                        {"id": "subpage_cards", "anchor": "sp"},
                        {"id": "provenance_drawer", "anchor": "src"},
                    ]
                },
                "tldr": "**Topic.**",
                "overview": "Overview.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:subpage_cards>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    cluster_arg = {
        "title": "T",
        "member_facts": facts,
        "child_count": child_count,
    }
    signals = compute_signals(cluster=cluster_arg)
    render_inputs = {
        "facts": facts,
        "decisions": [],
        "open_questions": [],
        "children": sub_children,
    }

    out = await compile_topic_page_modular(
        title="T",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=facts[:8],
        top_people=[],
        llm=stub_llm,
    )

    by_id = {m["id"]: m for m in out.modules}
    assert "subpage_cards" in by_id, f"child_count={child_count}: subpage_cards must fire (>=2)"


@pytest.mark.parametrize("fact_count", [15, 25, 50])
@pytest.mark.asyncio
async def test_boundary_and_far_above_threshold_route_modular(fact_count: int) -> None:
    """Cover the boundary (=15, exactly at threshold) + far-above
    (25, 50) range. All three sizes must produce frontend-renderer
    ``key_facts`` (no legacy table)."""
    facts = _make_facts(fact_count)

    async def stub_llm(prompt: str) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "summary"},
                        {"id": "key_facts", "anchor": "kf"},
                        {"id": "provenance_drawer", "anchor": "src"},
                    ]
                },
                "tldr": "**Topic.**",
                "overview": "Overview.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    cluster_arg = {"title": "T", "member_facts": facts, "child_count": 0}
    signals = compute_signals(cluster=cluster_arg)
    render_inputs = {"facts": facts, "decisions": [], "open_questions": []}

    out = await compile_topic_page_modular(
        title="T",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=facts[:8],
        top_people=[],
        llm=stub_llm,
    )

    by_id = {m["id"]: m for m in out.modules}
    assert by_id["key_facts"]["data"]["renderer_kind"] == "frontend", (
        f"fact_count={fact_count}: key_facts must be frontend renderer "
        "so the WikiTab mounts KeyFactsModule v2 cards instead of "
        "falling back to WikiMarkdown over page.content"
    )
    # Modular plan is non-empty so the frontend uses ModuleRenderer
    # rather than the markdown fallback.
    assert len(out.modules) >= 3
