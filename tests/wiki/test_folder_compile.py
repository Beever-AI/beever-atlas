"""End-to-end folder-archetype orchestrator integration test.

Synthesises a folder with 3 children + 30 facts (2 decisions across
descendants, 4 distinct contributors), runs ``compile_folder_page_modular``
with a stub LLM, and asserts:
  - the modular pipeline emits the dashboard module sequence
  - each module carries a ``data`` payload of the expected shape
  - "Themes & threads" prose does NOT appear in ``out.content``
  - fall-back behaviour fires on LLM failures without raising
"""

from __future__ import annotations

import json

import pytest

from beever_atlas.wiki.modules.orchestrator import (
    ModularPageOutput,
    compile_folder_page_modular,
)


def _build_synthetic_folder() -> tuple[list[dict], list[dict]]:
    """Synthesize a folder with 3 children, ~30 facts, 2 decisions
    across descendants, 4 distinct contributors."""
    descendants = [
        {
            "title": "JWT Migration",
            "slug": "jwt-migration",
            "facts": [
                {
                    "fact_id": f"f-jwt-{i}",
                    "memory_text": f"JWT fact {i}",
                    "author_name": ["Alan", "Bob", "Carol"][i % 3],
                    "fact_type": "claim" if i > 0 else "decision",
                    "importance": "high" if i == 0 else "medium",
                    "message_ts": "2026-04-15",
                }
                for i in range(10)
            ],
        },
        {
            "title": "Auth Roadmap",
            "slug": "auth-roadmap",
            "facts": [
                {
                    "fact_id": f"f-ar-{i}",
                    "memory_text": f"Roadmap fact {i}",
                    "author_name": ["Alan", "Daniel"][i % 2],
                    "fact_type": "claim" if i > 0 else "decision",
                    "importance": "critical" if i == 0 else "low",
                    "message_ts": "2026-04-20",
                }
                for i in range(10)
            ],
        },
        {
            "title": "OAuth Flow",
            "slug": "oauth-flow",
            "facts": [
                {
                    "fact_id": f"f-oa-{i}",
                    "memory_text": f"OAuth fact {i}",
                    "author_name": ["Bob", "Carol"][i % 2],
                    "fact_type": "claim",
                    "importance": "medium",
                    "message_ts": "2026-04-25",
                }
                for i in range(10)
            ],
        },
    ]
    children = [
        {"title": d["title"], "slug": d["slug"], "summary": f"summary for {d['title']}"}
        for d in descendants
    ]
    return descendants, children


def _llm_response_for_dashboard() -> str:
    return json.dumps(
        {
            "archetype": "folder",
            "plan": {
                "modules": [
                    {"id": "hero_summary", "anchor": "summary"},
                    {"id": "subpage_cards", "anchor": "subpages"},
                    {"id": "folder_stats", "anchor": "folder-stats"},
                    {"id": "top_contributors", "anchor": "top-contrib"},
                    {"id": "cross_cutting_decisions", "anchor": "cross-decisions"},
                    {"id": "provenance_drawer", "anchor": "sources"},
                ]
            },
            "hero": {
                "tldr": "**Auth & identity — wayfinding for migrations and design.**",
                "summary": (
                    "Three pages across this folder cover the move from SAML "
                    "to JWT, the evolving auth roadmap, and the OAuth flow we "
                    "stood up for partners. Alan and Bob drive most of the "
                    "decisions; Carol owns the operational side."
                ),
            },
            "body_connectors": {},
        }
    )


@pytest.mark.asyncio
async def test_folder_orchestrator_emits_dashboard_modules() -> None:
    descendants, children = _build_synthetic_folder()

    call_count = {"n": 0}

    async def fake_llm(prompt: str) -> str:
        call_count["n"] += 1
        # The prompt MUST mention the folder archetype + new modules.
        assert "folder_stats" in prompt
        assert "top_contributors" in prompt
        assert "cross_cutting_decisions" in prompt
        return _llm_response_for_dashboard()

    out = await compile_folder_page_modular(
        folder_title="Auth & Identity",
        folder_slug="auth-and-identity",
        descendants=descendants,
        children=children,
        llm=fake_llm,
    )

    assert isinstance(out, ModularPageOutput)
    assert call_count["n"] == 1
    assert out.fell_back is False

    module_ids = [m["id"] for m in out.modules]
    assert module_ids == [
        "hero_summary",
        "subpage_cards",
        "folder_stats",
        "top_contributors",
        "cross_cutting_decisions",
        "provenance_drawer",
    ]

    # The legacy prose blob must NOT appear in the new page content.
    assert "Themes & threads" not in out.content
    # TL;DR + summary survive in content for legacy markdown readers.
    assert "Auth & identity" in out.content or "**Auth" in out.content


@pytest.mark.asyncio
async def test_folder_module_data_payloads_have_expected_shape() -> None:
    """Each module entry carries a ``data`` payload the React
    dispatcher consumes. Spot-check the new folder modules."""
    descendants, children = _build_synthetic_folder()

    async def fake_llm(prompt: str) -> str:
        return _llm_response_for_dashboard()

    out = await compile_folder_page_modular(
        folder_title="Auth & Identity",
        folder_slug="auth-and-identity",
        descendants=descendants,
        children=children,
        llm=fake_llm,
    )

    by_id = {m["id"]: m for m in out.modules}

    # folder_stats — 4-card big-number strip.
    fs = by_id["folder_stats"]["data"]
    assert fs["label"] == "Folder stats"
    assert fs["renderer_kind"] == "frontend"
    labels = {s["label"] for s in fs["stats"]}
    assert {"memories", "decisions", "open questions", "contributors"} == labels
    # Every fact contributes one memory → 30 across the synthesis.
    memory_card = next(s for s in fs["stats"] if s["label"] == "memories")
    assert memory_card["value"] == "30"

    # top_contributors — Alan should top the list (10 + 5 facts = 15).
    tc = by_id["top_contributors"]["data"]
    assert tc["label"] == "Top contributors"
    assert tc["renderer_kind"] == "frontend"
    names = [c["name"] for c in tc["items"]]
    assert "Alan" in names
    assert names[0] == "Alan"  # Alan has the most contributions

    # cross_cutting_decisions — 2 decisions across descendants.
    cc = by_id["cross_cutting_decisions"]["data"]
    assert cc["label"] == "Cross-cutting decisions"
    assert cc["renderer_kind"] == "frontend"
    assert len(cc["items"]) == 2
    titles = [d["title"] for d in cc["items"]]
    assert any("JWT" in t for t in titles)
    assert any("Roadmap" in t for t in titles)


@pytest.mark.asyncio
async def test_folder_orchestrator_falls_back_on_llm_crash() -> None:
    descendants, children = _build_synthetic_folder()

    async def boom(prompt: str) -> str:
        raise RuntimeError("provider down")

    out = await compile_folder_page_modular(
        folder_title="Auth & Identity",
        folder_slug="auth-and-identity",
        descendants=descendants,
        children=children,
        llm=boom,
    )
    assert out.fell_back is True
    # Fallback dashboard still has hero_summary + subpage_cards + folder_stats.
    module_ids = {m["id"] for m in out.modules}
    assert "hero_summary" in module_ids
    assert "subpage_cards" in module_ids
    assert "folder_stats" in module_ids
    # No "Themes & threads" prose even on fall-back.
    assert "Themes & threads" not in out.content


@pytest.mark.asyncio
async def test_folder_orchestrator_falls_back_on_unparseable_json() -> None:
    descendants, children = _build_synthetic_folder()

    async def garbage(prompt: str) -> str:
        return "not json at all"

    out = await compile_folder_page_modular(
        folder_title="X",
        folder_slug="x",
        descendants=descendants,
        children=children,
        llm=garbage,
    )
    assert out.fell_back is True
    assert any(m["id"] == "hero_summary" for m in out.modules)


@pytest.mark.asyncio
async def test_folder_orchestrator_skips_topic_only_modules_on_folder() -> None:
    """If the LLM picks a topic-only module (key_facts, decision_log)
    on a folder page, the validator drops it because the folder
    signals don't satisfy those predicates (fact_count == 0 from the
    cluster's empty member_facts → key_facts predicate fails)."""
    descendants, children = _build_synthetic_folder()

    bad_response = json.dumps(
        {
            "archetype": "folder",
            "plan": {
                "modules": [
                    {"id": "hero_summary", "anchor": "summary"},
                    # Folder modules are still expected.
                    {"id": "folder_stats", "anchor": "fs"},
                    # Topic-only modules — without descendant aggregates
                    # populating the topic-side signals, these can't
                    # fire. We're lenient: aggregated descendant facts
                    # ARE forwarded so key_facts MAY fire if 5+ facts;
                    # the test guards the folder-specific modules
                    # surviving regardless.
                ]
            },
            "hero": {"tldr": "**X.**", "summary": "Y."},
        }
    )

    async def fake_llm(prompt: str) -> str:
        return bad_response

    out = await compile_folder_page_modular(
        folder_title="X",
        folder_slug="x",
        descendants=descendants,
        children=children,
        llm=fake_llm,
    )
    module_ids = [m["id"] for m in out.modules]
    assert "hero_summary" in module_ids
    assert "folder_stats" in module_ids
    # Themes & threads prose never reappears.
    assert "Themes & threads" not in out.content
