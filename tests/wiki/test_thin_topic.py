"""Tests for Phase 4 thin-topic routing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.models.domain import AtomicFact
from beever_atlas.wiki.compiler import WikiCompiler
from beever_atlas.wiki.schemas import CompiledPageContent


def _mk_fact(i: int) -> AtomicFact:
    return AtomicFact(
        id=f"f{i}",
        channel_id="c",
        source_message_id=f"m{i}",
        message_ts="1",
        author_id="u",
        author_name="alice",
        memory_text=f"fact {i}",
        topic_tags=[],
        fact_type="claim",
        importance="high",
        quality_score=0.9,
    )


def _make_compiler() -> WikiCompiler:
    provider = MagicMock()
    provider.get_model_string.return_value = "gemini-2.5-flash"
    with patch("beever_atlas.wiki.compiler.get_llm_provider", return_value=provider):
        return WikiCompiler()


def _cluster(n_facts: int = 3):
    return SimpleNamespace(
        id="c1",
        title="Small Topic",
        summary="summary",
        current_state="",
        open_questions=[],
        impact_note="",
        topic_tags=[],
        date_range_start="",
        date_range_end="",
        authors=[],
        member_count=n_facts,
        key_facts=[
            {
                "memory_text": "f1",
                "author_name": "a",
                "fact_type": "claim",
                "importance": 0.9,
                "quality_score": 0.9,
            },
        ],
        decisions=[],
        people=[],
        technologies=[],
        projects=[],
        key_entities=[],
        key_relationships=[],
        related_cluster_ids=[],
    )


def _gathered(facts: list) -> dict:
    return {"cluster_facts": {"c1": facts}, "clusters": []}


@pytest.mark.asyncio
async def test_thin_topic_falls_back_to_compile_thin_topic_when_modular_fails() -> None:
    """Post v1/v2 unification: modular is tried FIRST for every topic
    page. The thin-topic legacy path remains a fallback ONLY when the
    modular orchestrator returns None (catastrophic LLM/parse failure)
    AND the cluster is below the thin threshold AND v2 is on."""
    compiler = _make_compiler()
    cluster = _cluster(3)
    facts = [_mk_fact(i) for i in range(3)]

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_compiler_v2 = True
        # Force modular to "fail" (return None) so the thin fallback fires.
        with patch.object(
            compiler, "_try_compile_topic_modular", new=AsyncMock(return_value=None)
        ) as modular_mock:
            with patch.object(compiler, "_compile_thin_topic", new=AsyncMock()) as thin_mock:
                thin_mock.return_value = "sentinel"
                res = await compiler._compile_topic_page(cluster, _gathered(facts))
                assert res == "sentinel"
                modular_mock.assert_awaited_once()
                thin_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_thin_topic_uses_modular_path_first() -> None:
    """A 3-fact cluster MUST go through the modular path before any
    legacy fallback — every topic page emits frontend-renderer modules
    in v2."""
    compiler = _make_compiler()
    cluster = _cluster(3)
    facts = [_mk_fact(i) for i in range(3)]

    sentinel_page = MagicMock(name="modular_page")
    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_compiler_v2 = True
        with patch.object(
            compiler, "_try_compile_topic_modular", new=AsyncMock(return_value=sentinel_page)
        ) as modular_mock:
            with patch.object(compiler, "_compile_thin_topic", new=AsyncMock()) as thin_mock:
                res = await compiler._compile_topic_page(cluster, _gathered(facts))
                assert res is sentinel_page
                modular_mock.assert_awaited_once()
                thin_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_thin_topic_page_has_no_diagram() -> None:
    compiler = _make_compiler()
    cluster = _cluster(3)
    facts = [_mk_fact(i) for i in range(3)]

    async def _fake_call_llm(
        prompt: str, max_retries: int = 1, page_kind: str = "topic", **_kwargs
    ):
        return CompiledPageContent(
            content="**TL;DR** Small topic.\n\n<<KEY_FACTS_TABLE>>\n\nSummary paragraph.",
            summary="s",
        )

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_compiler_v2 = True
        mock_settings.return_value.wiki_parse_hardening = True
        mock_settings.return_value.wiki_token_budget_v2 = True
        with patch.object(WikiCompiler, "_call_llm", new=_fake_call_llm):
            page = await compiler._compile_thin_topic(cluster, _gathered(facts))

    assert "```mermaid" not in page.content
    assert "Open Questions" not in page.content
    assert "See Also" not in page.content
    # Deterministic table substituted.
    assert "<<KEY_FACTS_TABLE>>" not in page.content
    assert "| Fact | Source | Type | Importance |" in page.content
