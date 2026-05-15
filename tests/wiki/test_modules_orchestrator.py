"""End-to-end orchestrator integration tests (single-call architecture).

Synthesizes a cluster with predictable signals (decisions, entities,
unanswered questions, media), runs the full pipeline with a stub LLM,
and asserts the resulting page:
- contains the expected modules in body order
- substitutes module markers with rendered module content
- never raises on edge cases (substitution failure, LLM crash, empty
  plan)
- persists a ``modules`` plan suitable for the maintainer
- always uses exactly one LLM call per topic page (cost contract)
"""

from __future__ import annotations

import json

import pytest

from beever_atlas.wiki.modules.orchestrator import (
    ModularPageOutput,
    compile_topic_page_modular,
)
from beever_atlas.wiki.modules.planner import compute_signals


def _signals_for_test() -> dict:
    """Build a signals dict that satisfies several module predicates:
    - 6 facts → key_facts
    - 3 decisions → decision_log
    - 3 entities + 5 edges → entity_diagram
    - 1 open question → open_questions
    """
    cluster = {
        "title": "JWT Migration",
        "member_facts": [
            {"fact_type": "decision", "author_name": "A", "date": "2026-04-01"},
            {"fact_type": "claim", "author_name": "B"},
            {"fact_type": "claim", "author_name": "C"},
            {"fact_type": "opinion", "author_name": "D"},
            {"fact_type": "event", "date": "2026-04-15"},
            {"fact_type": "event", "date": "2026-04-30"},
        ],
    }
    return compute_signals(
        cluster=cluster,
        decisions=[{"decision": "X"}, {"decision": "Y"}, {"decision": "Z"}],
        entities=[{"id": "E1"}, {"id": "E2"}, {"id": "E3"}],
        relationships=[
            # Diverse verbs so the suppression pass keeps the diagram
            # (distinct_edge_verbs ≥ 2 + no single dominant pair).
            {"from": "E1", "to": "E2", "label": "replaces"},
            {"from": "E2", "to": "E3", "label": "supports"},
            {"from": "E3", "to": "E1", "label": "issues"},
            {"from": "E1", "to": "E3", "label": "validates against"},
            {"from": "E2", "to": "E1", "label": "deprecated by"},
        ],
        open_questions=[{"question": "What about token rotation?"}],
    )


def _render_inputs_for_test() -> dict:
    """Per-module data payload — the orchestrator slices this into
    each module's expected input shape."""
    return {
        "facts": [
            {"memory_text": "Token TTL set to 24h", "fact_type": "decision", "importance": 8},
            {"memory_text": "JWT replaces SAML", "fact_type": "decision", "importance": 9},
        ],
        "decisions": [
            {"decision": "Adopt JWT", "status": "active", "made_by": "Jacky", "date": "2026-04-15"},
            {
                "decision": "Drop SAML",
                "status": "active",
                "made_by": "Thomas",
                "date": "2026-04-15",
            },
            {"decision": "24h TTL", "status": "pending", "made_by": "Alan", "date": "2026-04-20"},
        ],
        "entities": [
            {"id": "E1", "label": "JWT"},
            {"id": "E2", "label": "SAML"},
            {"id": "E3", "label": "Auth Service"},
        ],
        "relationships": [
            {"from": "E1", "to": "E2", "label": "replaces"},
            {"from": "E2", "to": "E3", "label": "supports"},
            {"from": "E3", "to": "E1", "label": "issues"},
            {"from": "E1", "to": "E3", "label": "validates against"},
            {"from": "E2", "to": "E1", "label": "deprecated by"},
        ],
        "open_questions": [
            {"question": "Token rotation strategy?", "raised": "2026-04-12"},
        ],
    }


@pytest.mark.asyncio
async def test_orchestrator_picks_and_renders_expected_modules() -> None:
    """Single LLM call returns plan + tldr + overview + body. The
    orchestrator validates the plan, renders modules deterministically,
    substitutes markers, and assembles the final content."""
    llm_response = json.dumps(
        {
            "plan": {
                "modules": [
                    {"id": "key_facts", "anchor": "kf"},
                    {"id": "decision_log", "anchor": "dl"},
                    {"id": "entity_diagram", "anchor": "ed"},
                    {"id": "open_questions", "anchor": "oq"},
                ]
            },
            "tldr": "**JWT replaces SAML for session auth.**",
            "overview": "The team migrated from SAML to JWT in April 2026 after evaluating cost, latency, and integration burden.",
            "body": (
                "Three rollout decisions shaped the schedule:\n\n"
                "<<MODULE:key_facts>>\n\n"
                "Each decision tied to an explicit timeline:\n\n"
                "<<MODULE:decision_log>>\n\n"
                "The migration touched three subsystems:\n\n"
                "<<MODULE:entity_diagram>>\n\n"
                "Two unresolved questions remain:\n\n"
                "<<MODULE:open_questions>>"
            ),
        }
    )

    call_count = {"n": 0}

    async def fake_llm(prompt: str) -> str:
        call_count["n"] += 1
        return llm_response

    out = await compile_topic_page_modular(
        title="JWT Migration",
        summary="Auth changeover",
        signals=_signals_for_test(),
        render_inputs=_render_inputs_for_test(),
        top_facts=[],
        top_people=[],
        date_range_start="2026-04-01",
        date_range_end="2026-04-30",
        llm=fake_llm,
    )

    assert isinstance(out, ModularPageOutput)
    # Cost contract: exactly ONE LLM call per topic page.
    assert call_count["n"] == 1, (
        f"expected single-call architecture; LLM was invoked {call_count['n']} times"
    )
    assert out.fell_back is False

    # Modules persisted in plan order — maintainer reads this.
    module_ids = [m["id"] for m in out.modules]
    assert module_ids == ["key_facts", "decision_log", "entity_diagram", "open_questions"]
    assert out.planner_module_count == 4
    assert out.rendered_module_count == 4

    # Content has TL;DR + Overview + body. Body has every module
    # rendered (no raw markers left).
    assert out.content.startswith("**JWT replaces SAML for session auth.**")
    assert "April 2026" in out.content  # overview prose
    assert "<<MODULE:" not in out.content  # all markers substituted
    assert "Adopt JWT" in out.content  # decision_log row
    assert "Token rotation" in out.content  # open_questions
    assert "```mermaid" in out.content  # entity_diagram fence

    # Body order preserved — facts before decisions before entities
    # before open questions.
    pos_kf = out.content.index("Token TTL")
    pos_dl = out.content.index("Adopt JWT")
    pos_ed = out.content.index("```mermaid")
    pos_oq = out.content.index("Token rotation")
    assert pos_kf < pos_dl < pos_ed < pos_oq


@pytest.mark.asyncio
async def test_orchestrator_falls_back_on_unknown_module_marker() -> None:
    """LLM emits a marker for an unknown module ID — substitution
    raises, orchestrator catches, content degrades to TL;DR + overview
    only with fell_back=True."""
    llm_response = json.dumps(
        {
            "plan": {"modules": [{"id": "key_facts", "anchor": "kf"}]},
            "tldr": "**X.**",
            "overview": "Y.",
            "body": "Intro.\n\n<<MODULE:totally_made_up>>\n\nOutro.",
        }
    )

    async def fake_llm(prompt: str) -> str:
        return llm_response

    out = await compile_topic_page_modular(
        title="T",
        summary="",
        signals=_signals_for_test(),
        render_inputs=_render_inputs_for_test(),
        top_facts=[],
        top_people=[],
        llm=fake_llm,
    )
    assert out.fell_back is True
    # TL;DR + overview survive; body dropped.
    assert "**X.**" in out.content
    assert "Y." in out.content
    assert "Intro" not in out.content


@pytest.mark.asyncio
async def test_orchestrator_falls_back_on_llm_crash() -> None:
    """LLM call raises — orchestrator returns the catastrophic
    fallback (single key_facts module rendered from render_inputs)."""

    async def boom(prompt: str) -> str:
        raise RuntimeError("provider down")

    out = await compile_topic_page_modular(
        title="T",
        summary="",
        signals=_signals_for_test(),
        render_inputs=_render_inputs_for_test(),
        top_facts=[],
        top_people=[],
        llm=boom,
    )
    assert out.fell_back is True
    # Catastrophic fallback rendered key_facts from render_inputs.
    assert "Token TTL" in out.content
    assert len(out.modules) == 1
    assert out.modules[0]["id"] == "key_facts"


@pytest.mark.asyncio
async def test_orchestrator_falls_back_on_unparseable_json() -> None:
    """LLM returns garbage — fallback kicks in."""

    async def garbage(prompt: str) -> str:
        return "not json at all"

    out = await compile_topic_page_modular(
        title="T",
        summary="",
        signals=_signals_for_test(),
        render_inputs=_render_inputs_for_test(),
        top_facts=[],
        top_people=[],
        llm=garbage,
    )
    assert out.fell_back is True
    assert len(out.modules) == 1
    assert out.modules[0]["id"] == "key_facts"


@pytest.mark.asyncio
async def test_orchestrator_falls_back_when_plan_validates_to_empty() -> None:
    """LLM picks modules whose criteria all fail. Validator drops
    everything → fallback kicks in."""
    llm_response = json.dumps(
        {
            "plan": {
                "modules": [
                    {"id": "comparison_matrix"},  # needs alts ≥ 2, signals has 0
                    {"id": "timeline"},  # needs ≥ 4 events spanning ≥ 14d, signals has 2
                ]
            },
            "tldr": "**X.**",
            "overview": "Y.",
            "body": "<<MODULE:comparison_matrix>>\n\n<<MODULE:timeline>>",
        }
    )

    async def fake_llm(prompt: str) -> str:
        return llm_response

    out = await compile_topic_page_modular(
        title="T",
        summary="",
        signals=_signals_for_test(),
        render_inputs=_render_inputs_for_test(),
        top_facts=[],
        top_people=[],
        llm=fake_llm,
    )
    # Validator dropped both modules → empty plan → catastrophic
    # fallback to single key_facts.
    assert out.fell_back is True
    assert len(out.modules) == 1
    assert out.modules[0]["id"] == "key_facts"


@pytest.mark.asyncio
async def test_orchestrator_attaches_data_payload_for_frontend_dispatcher() -> None:
    """Each module entry MUST carry a ``data`` payload (label,
    renderer_kind + module-specific fields) so the frontend
    dispatcher can render module-by-module without re-parsing
    page.content. ``key_facts`` v2 is a frontend renderer — its
    payload is a structured ``items`` list, not pre-rendered
    markdown. The legacy markdown table still ships in the body
    for the page.content path."""
    llm_response = json.dumps(
        {
            "plan": {"modules": [{"id": "key_facts", "anchor": "kf"}]},
            "tldr": "**X.**",
            "overview": "Y.",
            "body": "<<MODULE:key_facts>>",
        }
    )

    async def fake_llm(prompt: str) -> str:
        return llm_response

    out = await compile_topic_page_modular(
        title="T",
        summary="",
        signals=_signals_for_test(),
        render_inputs=_render_inputs_for_test(),
        top_facts=[],
        top_people=[],
        llm=fake_llm,
    )
    assert out.fell_back is False
    assert len(out.modules) == 1
    data = out.modules[0].get("data", {})
    assert data.get("label") == "Key Facts"
    assert data.get("renderer_kind") == "frontend"
    items = data.get("items") or []
    assert isinstance(items, list)
    assert len(items) >= 1
    # First-sentence title surfaces from memory_text.
    assert any("Token TTL" in (it.get("title") or "") for it in items)
    # Body marker still substitutes the legacy markdown table for
    # the page.content path so existing rendering continues to work.
    assert "Token TTL" in out.content
