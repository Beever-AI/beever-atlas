"""Tests for the ``hero_summary`` module — frontend renderer.

Covers:
  - always-eligible predicate (gated only by fact_count ≥ 1)
  - planner-output mapping (tldr + overview from the LLM response
    propagate into the module data unchanged)
  - highlights computation (critical_count / decision_count /
    open_question_count / tension_count)

Pure unit tests — no LLM, network, or DB.
"""

from __future__ import annotations

import json

import pytest

from beever_atlas.wiki.modules import MODULE_CATALOG
from beever_atlas.wiki.modules.hero_summary import build_hero_summary_data
from beever_atlas.wiki.modules.orchestrator import compile_topic_page_modular
from beever_atlas.wiki.modules.planner import compute_signals


# ---------------------------------------------------------------------------
# Catalog entry — always-eligible predicate
# ---------------------------------------------------------------------------


def test_hero_summary_in_catalog() -> None:
    assert "hero_summary" in MODULE_CATALOG
    spec = MODULE_CATALOG["hero_summary"]
    assert spec.id == "hero_summary"
    assert spec.label == "Summary"
    assert spec.renderer_kind == "frontend"


def test_hero_summary_eligible_with_one_fact() -> None:
    spec = MODULE_CATALOG["hero_summary"]
    assert spec.eligible({"fact_count": 1}) is True
    assert spec.eligible({"fact_count": 5}) is True
    assert spec.eligible({"fact_count": 100}) is True


def test_hero_summary_ineligible_with_zero_facts() -> None:
    """fact_count == 0 is the one case where hero_summary is skipped
    (no content to summarise)."""
    spec = MODULE_CATALOG["hero_summary"]
    assert spec.eligible({"fact_count": 0}) is False
    assert spec.eligible({}) is False  # missing key defaults to 0


# ---------------------------------------------------------------------------
# build_hero_summary_data — pure builder
# ---------------------------------------------------------------------------


def test_build_hero_summary_data_top_level_shape() -> None:
    out = build_hero_summary_data(
        tldr="**X replaces Y.**",
        overview="Migration ran in Q1 with 24h TTL.",
        signals={"decision_count": 3, "open_question_count": 1},
        facts=[],
    )
    assert out["label"] == "Summary"
    assert out["renderer_kind"] == "frontend"
    assert "tldr" in out
    assert "summary" in out
    assert "highlights" in out
    h = out["highlights"]
    assert set(h.keys()) == {
        "critical_count",
        "decision_count",
        "open_question_count",
        "tension_count",
    }


def test_build_hero_summary_data_passes_tldr_and_summary_through() -> None:
    """Planner-output mapping: tldr + overview from the LLM response
    propagate into the module data unchanged (whitespace-trimmed)."""
    out = build_hero_summary_data(
        tldr="**Migration shipped clean.**",
        overview=(
            "The team finished the JWT/SAML migration in Q1 with 24h "
            "TTL. Production rollout starts March 1."
        ),
        signals={},
        facts=[],
    )
    assert out["tldr"] == "**Migration shipped clean.**"
    assert "Q1" in out["summary"]


def test_build_hero_summary_data_strips_surrounding_whitespace() -> None:
    out = build_hero_summary_data(
        tldr="   **Bold.**   ",
        overview="\n\nProse goes here.\n",
        signals={},
        facts=[],
    )
    assert out["tldr"] == "**Bold.**"
    assert out["summary"] == "Prose goes here."


# ---------------------------------------------------------------------------
# Highlights computation
# ---------------------------------------------------------------------------


def test_build_hero_summary_data_critical_count_from_facts() -> None:
    """Critical fact count comes from the facts list (importance ≥ 9
    OR explicit "critical" string), mirroring the threshold the
    KeyFactsModule uses to promote critical facts to the top strip."""
    facts = [
        {"importance": "critical"},
        {"importance": 9},
        {"importance": 10},
        {"importance": "high"},  # not critical
        {"importance": 7},  # not critical
        {"importance": "medium"},
    ]
    out = build_hero_summary_data(
        tldr="x",
        overview="y",
        signals={},
        facts=facts,
    )
    assert out["highlights"]["critical_count"] == 3


def test_build_hero_summary_data_decision_and_question_counts_from_signals() -> None:
    out = build_hero_summary_data(
        tldr="x",
        overview="y",
        signals={
            "decision_count": 4,
            "open_question_count": 2,
            "conflict_count": 1,
        },
        facts=[],
    )
    h = out["highlights"]
    assert h["decision_count"] == 4
    assert h["open_question_count"] == 2
    assert h["tension_count"] == 1


def test_build_hero_summary_data_zero_counts_when_signals_empty() -> None:
    out = build_hero_summary_data(tldr="x", overview="y", signals={}, facts=[])
    h = out["highlights"]
    assert h["critical_count"] == 0
    assert h["decision_count"] == 0
    assert h["open_question_count"] == 0
    assert h["tension_count"] == 0


# ---------------------------------------------------------------------------
# Robustness — builder is total over malformed input
# ---------------------------------------------------------------------------


def test_build_hero_summary_data_handles_none_inputs() -> None:
    out = build_hero_summary_data(
        tldr="",  # type: ignore[arg-type]
        overview="",  # type: ignore[arg-type]
        signals={},
        facts=None,
    )
    assert out["tldr"] == ""
    assert out["summary"] == ""
    assert out["highlights"]["critical_count"] == 0


# ---------------------------------------------------------------------------
# Orchestrator integration — hero_summary is populated from the
# planner LLM's tldr + overview fields.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_populates_hero_summary_data_from_planner_output() -> None:
    """When the LLM picks ``hero_summary`` as module #1, the
    orchestrator MUST attach the structured payload built from the
    LLM's own ``tldr`` + ``overview`` fields plus the topic
    signals."""
    cluster = {
        "title": "JWT Migration",
        "member_facts": [
            {"fact_type": "decision", "importance": 9},
            {"fact_type": "decision", "importance": 8},
            {"fact_type": "claim", "importance": 7},
            {"fact_type": "claim", "importance": 6},
            {"fact_type": "event", "importance": 5},
            {"fact_type": "event", "importance": 5},
        ],
    }
    signals = compute_signals(
        cluster=cluster,
        decisions=[{"decision": "Adopt JWT"}, {"decision": "24h TTL"}],
        open_questions=[{"question": "Token rotation strategy?"}],
    )
    render_inputs = {
        "facts": [
            {"memory_text": "JWT replaces SAML", "importance": 9},
            {"memory_text": "Token TTL set", "importance": 7},
        ],
        "decisions": [
            {"decision": "Adopt JWT", "status": "active"},
            {"decision": "24h TTL", "status": "active"},
        ],
        "open_questions": [{"question": "Rotation?", "raised": "2026-04-12"}],
    }

    llm_response = json.dumps(
        {
            "plan": {
                "modules": [
                    {"id": "hero_summary", "anchor": "hero"},
                    {"id": "key_facts", "anchor": "kf"},
                ]
            },
            "tldr": "**JWT replaces SAML for service auth.**",
            "overview": "The team migrated from SAML to JWT in Q1 2026.",
            "body": "<<MODULE:hero_summary>>\n\n<<MODULE:key_facts>>",
        }
    )

    async def fake_llm(prompt: str) -> str:
        return llm_response

    out = await compile_topic_page_modular(
        title="JWT Migration",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=[],
        top_people=[],
        llm=fake_llm,
    )

    by_id = {m["id"]: m for m in out.modules}
    assert "hero_summary" in by_id, "hero_summary module not in plan"
    hero_data = by_id["hero_summary"]["data"]
    assert hero_data["renderer_kind"] == "frontend"
    assert hero_data["label"] == "Summary"
    assert "JWT replaces SAML" in hero_data["tldr"]
    assert "Q1 2026" in hero_data["summary"]
    h = hero_data["highlights"]
    assert h["decision_count"] == 2  # from signals
    assert h["open_question_count"] == 1  # from signals
    assert h["critical_count"] == 1  # one fact has importance == 9 → critical


@pytest.mark.asyncio
async def test_orchestrator_hero_summary_module_marker_substitutes_to_empty() -> None:
    """When the LLM emits ``<<MODULE:hero_summary>>`` in the body,
    the substitution pass treats the marker as a frontend-only module
    (no markdown to splice). The marker is stripped silently — the
    React renderer takes over via the structured ``data`` payload."""
    cluster = {
        "title": "Test",
        "member_facts": [
            {"fact_type": "claim"},
            {"fact_type": "claim"},
        ],
    }
    signals = compute_signals(cluster=cluster)
    render_inputs = {
        "facts": [
            {"memory_text": "f1"},
            {"memory_text": "f2"},
        ],
    }

    async def fake_llm(prompt: str) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "hero"},
                        {"id": "key_facts", "anchor": "kf"},
                    ]
                },
                "tldr": "**T.**",
                "overview": "Some overview prose.",
                "body": "<<MODULE:hero_summary>>\n\n<<MODULE:key_facts>>",
            }
        )

    out = await compile_topic_page_modular(
        title="T",
        summary="",
        signals={**signals, "fact_count": 5},
        render_inputs=render_inputs,
        top_facts=[],
        top_people=[],
        llm=fake_llm,
    )
    # No raw markers leaked into the body.
    assert "<<MODULE:" not in out.content
    # TL;DR + overview show up at the top of page.content (existing
    # path), AND the structured hero_summary data is attached so the
    # React renderer has what it needs.
    assert out.content.startswith("**T.**")
    by_id = {m["id"]: m for m in out.modules}
    assert "hero_summary" in by_id
