"""Regression test for Bug 2 — every topic page MUST route through the
modular path regardless of fact count.

Pre-fix the compiler had three branches:
  - thin path (<5 facts)         → legacy ``THIN_TOPIC_PROMPT``
  - mid    (5–14 facts)          → modular
  - large  (≥15 facts)           → legacy ``TOPIC_PROMPT``

That produced inconsistent UX: small + mid pages got ``KeyFactsModule``
v2 cards; large pages got the GFM table baked into the page body.
After the fix, ALL pages with ≥1 fact emit module data with
``renderer_kind="frontend"`` for ``key_facts`` (and other
frontend-renderer modules the planner picks).

Also reproduces Bug 1's invariant: NO module's ``data`` payload may
contain ``<untrusted>`` substring (or any other safety wrapper) —
those are LLM-context defenses, not user-facing markup.
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


def _make_22_facts() -> list[dict[str, Any]]:
    """22 facts with realistic shape: each ``memory_text`` carries the
    ``<untrusted>...</untrusted>`` wrapper the compiler adds via
    ``wrap_untrusted`` before passing to the LLM. The fix ensures
    these wrappers do NOT survive into module ``data`` payloads."""
    out: list[dict[str, Any]] = []
    for i in range(22):
        out.append(
            {
                "memory_text": (
                    f"<untrusted>\nFact #{i}: a substantive observation "
                    f"about the topic that needs at least 100 chars so "
                    f"the title truncation gets exercised.\n</untrusted>"
                ),
                "fact_type": "claim" if i % 4 else "decision",
                "author_name": f"User{i % 4}",
                "date": f"2026-04-{(i % 28) + 1:02d}",
                "importance": 9 if i % 5 == 0 else 6,
                "quality_score": 0.9,
            }
        )
    return out


def _no_safety_marker_anywhere(payload: Any) -> tuple[bool, str]:
    """Recursively walk a JSON-shaped payload and assert no safety
    marker substring appears. Returns (clean, where_failed)."""
    SAFETY = (
        "<untrusted>",
        "</untrusted>",
        "<sanitized>",
        "</sanitized>",
        "<external>",
        "</external>",
    )
    if isinstance(payload, str):
        for tag in SAFETY:
            if tag in payload:
                return False, f"found {tag!r} in string {payload[:80]!r}"
        return True, ""
    if isinstance(payload, dict):
        for k, v in payload.items():
            ok, reason = _no_safety_marker_anywhere(v)
            if not ok:
                return False, f"key={k}: {reason}"
        return True, ""
    if isinstance(payload, list):
        for i, item in enumerate(payload):
            ok, reason = _no_safety_marker_anywhere(item)
            if not ok:
                return False, f"index={i}: {reason}"
        return True, ""
    return True, ""


@pytest.mark.asyncio
async def test_22_fact_cluster_emits_frontend_key_facts_module() -> None:
    """A 22-fact cluster (above the OLD legacy split threshold of 15)
    must produce a modular output with ``key_facts`` carrying
    ``renderer_kind == "frontend"`` — NOT a legacy markdown table.

    Stubs the LLM to return a plan with key_facts + hero_summary +
    provenance_drawer (the always-on trio when fact_count ≥ 5).
    """
    facts = _make_22_facts()

    async def stub_llm(prompt: str) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "summary"},
                        {"id": "key_facts", "anchor": "key-facts"},
                        {"id": "provenance_drawer", "anchor": "sources"},
                    ]
                },
                "tldr": "**Test topic with 22 facts.**",
                "overview": "Overview prose.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    cluster_arg = {
        "title": "22-Fact Topic",
        "member_facts": facts,
        "child_count": 0,
    }
    signals = compute_signals(cluster=cluster_arg)
    render_inputs = {"facts": facts, "decisions": [], "open_questions": []}

    out = await compile_topic_page_modular(
        title="22-Fact Topic",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=facts[:8],
        top_people=[{"name": "User0"}],
        date_range_start="2026-04-01",
        date_range_end="2026-04-28",
        llm=stub_llm,
    )

    assert isinstance(out, ModularPageOutput)
    assert out.fell_back is False, "modular path must succeed for 22-fact cluster"

    by_id = {m["id"]: m for m in out.modules}
    assert "key_facts" in by_id, "key_facts module MUST be present on 22-fact pages"

    # The crux of Bug 2: key_facts must be frontend-renderer-kind, NOT
    # the legacy markdown table baked into module.data.markdown.
    kf = by_id["key_facts"]["data"]
    assert kf["renderer_kind"] == "frontend", (
        f"key_facts should be frontend renderer; got {kf['renderer_kind']}. "
        "Did the routing fall back to legacy TOPIC_PROMPT path?"
    )
    assert "items" in kf, "frontend key_facts must carry an items list"
    assert isinstance(kf["items"], list)
    assert len(kf["items"]) >= 5, (
        f"22-fact cluster should yield ≥ 5 key_facts items; got {len(kf['items'])}"
    )


@pytest.mark.asyncio
async def test_no_safety_markers_in_any_module_data() -> None:
    """Bug 1 invariant: ``<untrusted>`` (or any other safety wrapper)
    MUST NOT appear in any module's ``data`` payload. Walks the entire
    output recursively and asserts no marker substring appears."""
    facts = _make_22_facts()

    async def stub_llm(prompt: str) -> str:
        # Echo the wrapped fact text into tldr/overview to make sure
        # those fields ALSO get stripped (the LLM sometimes mirrors
        # input text verbatim).
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "summary"},
                        {"id": "key_facts", "anchor": "kf"},
                        {"id": "decision_log", "anchor": "dl"},
                        {"id": "provenance_drawer", "anchor": "src"},
                    ]
                },
                # Even if the LLM regurgitates the wrapper into tldr,
                # the builder must strip it.
                "tldr": "<untrusted>**Mirror of fact text.**</untrusted>",
                "overview": "Plain overview.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:decision_log>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    cluster_arg = {
        "title": "Wrapper Strip Topic",
        "member_facts": facts,
        "child_count": 0,
    }
    signals = compute_signals(
        cluster=cluster_arg,
        decisions=[{"text": "<untrusted>some decision</untrusted>"}],
    )
    render_inputs = {
        "facts": facts,
        "decisions": [{"text": "some decision"}],
        "open_questions": [],
    }

    out = await compile_topic_page_modular(
        title="Wrapper Strip Topic",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=facts[:8],
        top_people=[{"name": "User0"}],
        date_range_start="2026-04-01",
        date_range_end="2026-04-28",
        llm=stub_llm,
    )

    # Walk every module's data payload — none may contain the wrapper.
    for m in out.modules:
        ok, reason = _no_safety_marker_anywhere(m.get("data") or {})
        assert ok, f"safety marker leaked into module {m['id']}.data — {reason}"


@pytest.mark.asyncio
async def test_thin_5_fact_cluster_also_routes_modular() -> None:
    """Bug 2 invariant — thin pages share the same modular path as
    rich pages. A 5-fact cluster's key_facts module must still be
    frontend-renderer-kind."""
    facts = _make_22_facts()[:5]

    async def stub_llm(prompt: str) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "s"},
                        {"id": "key_facts", "anchor": "kf"},
                        {"id": "provenance_drawer", "anchor": "src"},
                    ]
                },
                "tldr": "**Thin.**",
                "overview": "Overview.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    cluster_arg = {"title": "Thin", "member_facts": facts, "child_count": 0}
    signals = compute_signals(cluster=cluster_arg)
    render_inputs = {"facts": facts, "decisions": [], "open_questions": []}

    out = await compile_topic_page_modular(
        title="Thin",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=facts,
        top_people=[],
        llm=stub_llm,
    )

    by_id = {m["id"]: m for m in out.modules}
    assert by_id["key_facts"]["data"]["renderer_kind"] == "frontend"


# ---------------------------------------------------------------------------
# Round 4 — boundary + far-above-threshold coverage. Pre-fix the
# compiler-level routing only invoked the modular path for clusters
# strictly under TOPIC_SUBPAGE_THRESHOLD (15). Post-fix every fact
# count routes through the modular path; these cases pin the
# orchestrator's behaviour at the boundary (=15) and far above (25,
# 50) so a future regression that re-introduces a size-based legacy
# branch is caught immediately.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fact_count", [15, 25, 50])
@pytest.mark.asyncio
async def test_modular_path_runs_at_and_above_subpage_threshold(
    fact_count: int,
) -> None:
    """fact_count = 15 (boundary), 25, 50 — all must produce a
    frontend-renderer ``key_facts`` module. The boundary case is the
    important one: pre-fix `>=15` got routed to legacy
    ``TOPIC_PROMPT_V2``; post-fix it stays in the modular pipeline.
    """
    facts = _make_22_facts()
    # Stretch / trim to the requested size by repeating the seed
    # fixtures so the resulting list has the right count.
    while len(facts) < fact_count:
        facts.extend(_make_22_facts())
    facts = facts[:fact_count]

    async def stub_llm(prompt: str) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "hero_summary", "anchor": "s"},
                        {"id": "key_facts", "anchor": "kf"},
                        {"id": "provenance_drawer", "anchor": "src"},
                    ]
                },
                "tldr": "**Boundary.**",
                "overview": "Overview.",
                "body": (
                    "<<MODULE:hero_summary>>\n\n"
                    "<<MODULE:key_facts>>\n\n"
                    "<<MODULE:provenance_drawer>>"
                ),
            }
        )

    cluster_arg = {
        "title": f"Cluster-{fact_count}",
        "member_facts": facts,
        "child_count": 0,
    }
    signals = compute_signals(cluster=cluster_arg)
    render_inputs = {"facts": facts, "decisions": [], "open_questions": []}

    out = await compile_topic_page_modular(
        title=f"Cluster-{fact_count}",
        summary="",
        signals=signals,
        render_inputs=render_inputs,
        top_facts=facts[:8],
        top_people=[],
        llm=stub_llm,
    )

    assert isinstance(out, ModularPageOutput)
    assert out.modules, (
        f"fact_count={fact_count}: modular path produced empty modules "
        "list — frontend would fall back to WikiMarkdown(page.content)"
    )
    by_id = {m["id"]: m for m in out.modules}
    assert "key_facts" in by_id
    assert by_id["key_facts"]["data"]["renderer_kind"] == "frontend", (
        f"fact_count={fact_count}: key_facts must stay frontend renderer regardless of cluster size"
    )
