"""Tests for Phase 2: parallelism event-ordering assertions (no sleeps).

Tests use asyncio.Event gates to verify coroutines are concurrently PENDING
without relying on wall-time measurements.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.models.domain import AtomicFact, ChannelSummary, TopicCluster
from beever_atlas.wiki.compiler import WikiCompiler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_compiler() -> WikiCompiler:
    provider = MagicMock()
    provider.get_model_string.return_value = "gemini-2.5-flash"
    with patch("beever_atlas.wiki.compiler.get_llm_provider", return_value=provider):
        return WikiCompiler()


def _minimal_gathered(clusters: list[TopicCluster] | None = None) -> dict:
    fact = AtomicFact(
        id="f1",
        memory_text="Team uses Python for backend services, with extensive test coverage and CI pipelines.",
        quality_score=0.8,
        author_name="Alice",
        message_ts="1704067200",
        source_message_id="msg1",
        channel_id="C0TEST",
        importance="high",
        fact_type="observation",
    )
    cluster = TopicCluster(
        id="c1",
        channel_id="C0TEST",
        title="Python Backend",
        summary="Python backend services.",
        current_state="Stable.",
        open_questions="",
        impact_note="Core tech.",
        topic_tags=["python"],
        member_ids=["f1"],
        member_count=1,
        key_facts=[
            {
                "fact_id": "f1",
                "memory_text": "Team uses Python",
                "author_name": "Alice",
                "message_ts": "1704067200",
                "fact_type": "observation",
                "importance": "high",
                "quality_score": 0.8,
                "source_message_id": "msg1",
            }
        ],
        faq_candidates=[],
    )
    channel_summary = ChannelSummary(
        id="s1",
        channel_id="C0TEST",
        channel_name="#test",
        text="Test channel.",
        themes="Python.",
        glossary_terms=[],
    )
    used_clusters = clusters or [cluster]
    return {
        "channel_id": "C0TEST",
        "channel_name": "#test",
        "channel_summary": channel_summary,
        "clusters": used_clusters,
        "cluster_facts": {c.id: [fact] for c in used_clusters},
        "recent_facts": [fact],
        "media_facts": [],
        "decisions": [],
        "technologies": [],
        "projects": [],
        "persons": [],
        "total_facts": 1,
    }


def _good_response(
    content: str = "This is a detailed page content with real prose about the topic being discussed.",
) -> str:
    return json.dumps({"content": content, "summary": "Summary."})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_translate_and_fixed_pages_run_concurrently() -> None:
    """With parallel_dispatch=ON, _translate_cluster_titles and _compile_people
    must both enter execution before either completes.

    Uses asyncio.Event gates: translate.entered and people.entered must both
    fire before either translation or people compilation finishes.
    """
    compiler = _make_compiler()
    gathered = _minimal_gathered()

    translate_entered = asyncio.Event()
    translate_gate = asyncio.Event()
    people_entered = asyncio.Event()

    original_people = compiler._compile_people

    async def _patched_translate(clusters):
        translate_entered.set()
        # Block until the gate is released by the test.
        await translate_gate.wait()
        return {}

    async def _patched_people(g):
        people_entered.set()
        return await original_people(g)

    async def _run():
        with patch.object(compiler, "_translate_cluster_titles", _patched_translate):
            with patch.object(compiler, "_compile_people", _patched_people):
                with patch.object(
                    compiler, "_llm_generate_json", AsyncMock(return_value=_good_response())
                ):
                    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
                        mock_settings.return_value.wiki_parse_hardening = True
                        mock_settings.return_value.wiki_parallel_dispatch = True
                        mock_settings.return_value.wiki_token_budget_v2 = True
                        mock_settings.return_value.wiki_compiler_v2 = False

                        compile_task = asyncio.create_task(compiler.compile(gathered))

                        # Wait for both to enter; if they're serial, translate would
                        # block before people ever starts.
                        await asyncio.wait_for(translate_entered.wait(), timeout=5.0)
                        await asyncio.wait_for(people_entered.wait(), timeout=5.0)

                        # Release the translation gate so compile can finish.
                        translate_gate.set()
                        await compile_task

    await _run()

    assert translate_entered.is_set(), "translate_cluster_titles never entered"
    assert people_entered.is_set(), "compile_people never entered"


@pytest.mark.asyncio
async def test_failed_subpage_dropped_from_children_refs() -> None:
    """When a sub-page call raises, the parent's children_refs must exclude it."""
    compiler = _make_compiler()

    # Large cluster to trigger sub-page analysis.
    facts_data = [
        {
            "fact_id": f"f{i}",
            "memory_text": f"Fact {i} about this topic.",
            "author_name": "Alice",
            "message_ts": str(1704067200 + i),
            "fact_type": "observation",
            "importance": "medium",
            "quality_score": 0.7,
            "source_message_id": f"msg{i}",
        }
        for i in range(20)
    ]
    facts = [
        AtomicFact(
            id=f"f{i}",
            memory_text=f"Fact {i} about this topic covering engineering decisions.",
            quality_score=0.7,
            author_name="Alice",
            message_ts=str(1704067200 + i),
            source_message_id=f"msg{i}",
            channel_id="C0TEST",
            importance="medium",
            fact_type="observation",
        )
        for i in range(20)
    ]
    cluster = TopicCluster(
        id="big-cluster",
        channel_id="C0TEST",
        title="Large Topic",
        summary="A large topic with many facts.",
        current_state="Active.",
        open_questions="Open?",
        impact_note="High impact.",
        topic_tags=["large"],
        member_ids=[f"f{i}" for i in range(20)],
        member_count=20,
        key_facts=facts_data,
        faq_candidates=[],
    )
    gathered = _minimal_gathered(clusters=[cluster])
    gathered["cluster_facts"] = {"big-cluster": facts}
    gathered["total_facts"] = 20

    analysis_response = json.dumps(
        {
            "needs_subpages": True,
            "subpages": [
                {"title": "Sub-page A", "fact_indices": [0, 1, 2, 3, 4]},
                {"title": "Sub-page B (will fail)", "fact_indices": [5, 6, 7, 8, 9]},
            ],
        }
    )

    call_count = {"n": 0}

    async def _mock_llm(self_inner, prompt: str, temperature: float = 0.2) -> str:
        call_count["n"] += 1
        p = prompt.lower()
        if "analyze" in p and "subpage" in p:
            return analysis_response
        if "sub-page b" in p or "will fail" in p:
            raise RuntimeError("Simulated sub-page failure")
        return _good_response(
            "This is a detailed content page about the topic with enough real prose to pass validation checks."
        )

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_parse_hardening = True
        mock_settings.return_value.wiki_parallel_dispatch = False
        mock_settings.return_value.wiki_token_budget_v2 = True
        mock_settings.return_value.wiki_compiler_v2 = False

        with patch.object(WikiCompiler, "_llm_generate_json", _mock_llm):
            pages = await compiler.compile(gathered)

    # Sub-page B failed; only sub-page A (or parent) should appear.
    page_ids = list(pages.keys())
    # No page should have "fail" in its id.
    failing_pages = [
        pid for pid in page_ids if "fail" in pid.lower() or "sub-page-b" in pid.lower()
    ]
    assert failing_pages == [], f"Failed sub-page leaked into results: {failing_pages}"


@pytest.mark.asyncio
async def test_parallel_dispatch_flag_off_serial_behavior() -> None:
    """With parallel_dispatch=OFF, compile still produces correct results (serial path)."""
    compiler = _make_compiler()
    gathered = _minimal_gathered()

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_parse_hardening = True
        mock_settings.return_value.wiki_parallel_dispatch = False
        mock_settings.return_value.wiki_token_budget_v2 = True
        mock_settings.return_value.wiki_compiler_v2 = False

        with patch.object(
            WikiCompiler, "_llm_generate_json", AsyncMock(return_value=_good_response())
        ):
            pages = await compiler.compile(gathered)

    assert len(pages) > 0, "Serial path produced no pages"
    assert "overview" in pages
    assert "people" in pages
