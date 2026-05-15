"""End-to-end test for the adaptive-modules wiki pipeline.

Exercises the full flow: cluster signals → planner → per-module
deterministic renderers → marker substitution → assembled page +
persisted modules list. Includes media modules (hero, inline,
gallery, link card, PDF preview, video embed) to verify the
frontend-only data extraction path.

Privacy guarantee — every fixture uses synthetic / fictional data:
- Channel: "C_TEST_E2E"
- Project: "Acme Robotics SDK" (made up)
- People: Alice, Bob, Charlie, Dana, Eve, Frank (placeholder names)
- URLs: example.com / example.org subdomains
- No real Mattermost / Slack / GitHub / channel data is referenced.
"""

from __future__ import annotations

import json

import pytest

from beever_atlas.wiki.modules.orchestrator import (
    ModularPageOutput,
    compile_topic_page_modular,
)
from beever_atlas.wiki.modules.planner import compute_signals


# ---------------------------------------------------------------------------
# Synthetic data fixtures — fictional project, no real channel content.
# ---------------------------------------------------------------------------

_SYNTHETIC_FACTS = [
    {
        "memory_text": "Adopted JWT for service auth — replaces SAML",
        "fact_type": "decision",
        "author_name": "Alice",
        "date": "2026-01-10",
        "importance": 9,
        "quality_score": 0.95,
    },
    {
        "memory_text": "Token TTL set to 24 hours after security review",
        "fact_type": "decision",
        "author_name": "Bob",
        "date": "2026-01-15",
        "importance": 8,
        "quality_score": 0.92,
    },
    {
        "memory_text": "Auth service handles 50K req/min in load test",
        "fact_type": "event",
        "author_name": "Charlie",
        "date": "2026-01-20",
        "importance": 7,
        "quality_score": 0.88,
    },
    {
        "memory_text": "Refresh token rotation strategy still under discussion",
        "fact_type": "claim",
        "author_name": "Alice",
        "date": "2026-02-01",
        "importance": 6,
        "quality_score": 0.80,
    },
    {
        "memory_text": "Session storage moved from Redis to in-memory cache",
        "fact_type": "decision",
        "author_name": "Dana",
        "date": "2026-02-10",
        "importance": 8,
        "quality_score": 0.91,
    },
    {
        "memory_text": "Production rollout starts March 1",
        "fact_type": "event",
        "author_name": "Eve",
        "date": "2026-02-25",
        "importance": 9,
        "quality_score": 0.93,
    },
    {
        "memory_text": "Monitoring dashboard live for new auth metrics",
        "fact_type": "event",
        "author_name": "Frank",
        "date": "2026-03-05",
        "importance": 7,
        "quality_score": 0.86,
    },
]

_SYNTHETIC_DECISIONS = [
    {
        "decision": "Adopt JWT for service auth",
        "status": "active",
        "made_by": "Alice",
        "date": "2026-01-10",
    },
    {
        "decision": "24h token TTL",
        "status": "active",
        "made_by": "Bob",
        "date": "2026-01-15",
    },
    {
        "decision": "In-memory session cache (replace Redis)",
        "status": "active",
        "made_by": "Dana",
        "date": "2026-02-10",
    },
]

_SYNTHETIC_ENTITIES = [
    {"id": "JWT", "label": "JWT", "kind": "concept"},
    {"id": "SAML", "label": "SAML", "kind": "concept"},
    {"id": "AUTHSVC", "label": "Auth Service", "kind": "system"},
    {"id": "REDIS", "label": "Redis", "kind": "system"},
]

_SYNTHETIC_RELATIONSHIPS = [
    {"from": "JWT", "to": "SAML", "label": "replaces"},
    {"from": "AUTHSVC", "to": "JWT", "label": "issues"},
    {"from": "AUTHSVC", "to": "JWT", "label": "validates"},
    {"from": "AUTHSVC", "to": "REDIS", "label": "deprecates"},
    {"from": "JWT", "to": "AUTHSVC", "label": "consumed by"},
]

_SYNTHETIC_OPEN_QUESTIONS = [
    {"question": "How should refresh token rotation work?", "raised": "2026-02-01"},
    {"question": "Do we need audit logging for token issuance?", "raised": "2026-02-15"},
]

_SYNTHETIC_MEDIA = [
    {
        "id": "m_hero",
        "kind": "image",
        "url": "https://example.com/auth-architecture.png",
        "alt": "Acme Robotics SDK auth architecture",
        "title": "Acme Robotics SDK auth architecture",
        "caption": "JWT replaces SAML in the SDK auth flow.",
        "author": "Alice",
        "date": "2026-01-10",
        "is_hero": True,
        "referencing_fact_count": 4,
    },
    {
        "id": "m_inline_1",
        "kind": "image",
        "url": "https://example.com/load-test-results.png",
        "alt": "Load test results — 50K req/min",
        "caption": "Sustained 50K req/min in the load test.",
        "source_fact_id": "fact_3",
    },
    {
        "id": "m_gallery_1",
        "kind": "image",
        "url": "https://example.com/dashboard-screenshot-1.png",
        "alt": "Auth metrics dashboard, view 1",
    },
    {
        "id": "m_gallery_2",
        "kind": "image",
        "url": "https://example.com/dashboard-screenshot-2.png",
        "alt": "Auth metrics dashboard, view 2",
    },
    {
        "id": "m_gallery_3",
        "kind": "image",
        "url": "https://example.com/dashboard-screenshot-3.png",
        "alt": "Auth metrics dashboard, view 3",
    },
    {
        "id": "m_pdf",
        "kind": "pdf",
        "url": "https://example.org/security-review.pdf",
        "title": "Security Review — Q1 2026",
    },
    {
        "id": "m_video",
        "kind": "video",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "title": "Acme Robotics SDK demo (synthetic)",
    },
    {
        "id": "m_link",
        "kind": "link",
        "url": "https://example.org/jwt-best-practices",
        "title": "JWT best practices",
        "description": "External reference doc on JWT rotation.",
    },
]


def _build_signals() -> dict:
    """Compute realistic signals from the synthetic fixture."""
    cluster = {
        "title": "Acme Robotics SDK Auth Migration",
        "member_facts": _SYNTHETIC_FACTS,
        "child_count": 0,
    }
    return compute_signals(
        cluster=cluster,
        decisions=_SYNTHETIC_DECISIONS,
        entities=_SYNTHETIC_ENTITIES,
        relationships=_SYNTHETIC_RELATIONSHIPS,
        media=_SYNTHETIC_MEDIA,
        open_questions=_SYNTHETIC_OPEN_QUESTIONS,
        related_topics=[
            {"id": "topic-rate-limiting", "title": "Rate limiting strategy", "score": 0.55},
            {"id": "topic-audit-logging", "title": "Audit logging", "score": 0.42},
        ],
    )


def _build_render_inputs() -> dict:
    """Per-module data bag the orchestrator slices into each renderer."""
    return {
        "facts": _SYNTHETIC_FACTS,
        "decisions": _SYNTHETIC_DECISIONS,
        "events": [
            {"date": f["date"], "event": f["memory_text"], "citations": "[1]"}
            for f in _SYNTHETIC_FACTS
            if (f.get("fact_type") or "").lower() in {"event", "decision"}
        ],
        "entities": _SYNTHETIC_ENTITIES,
        "relationships": _SYNTHETIC_RELATIONSHIPS,
        "open_questions": _SYNTHETIC_OPEN_QUESTIONS,
        "related_topics": [
            {
                "title": "Rate limiting strategy",
                "slug": "topic-rate-limiting",
                "reason": "shares Auth Service entity",
            },
            {
                "title": "Audit logging",
                "slug": "topic-audit-logging",
                "reason": "shares contributor Alice",
            },
        ],
        "media": _SYNTHETIC_MEDIA,
    }


def _stub_llm_response() -> str:
    """Single LLM response covering the unified prompt — picks a
    realistic 6-module mix including media."""
    return json.dumps(
        {
            "plan": {
                "modules": [
                    {"id": "key_facts", "anchor": "kf"},
                    {"id": "decision_log", "anchor": "dl"},
                    {"id": "media_hero", "anchor": "mh"},
                    {"id": "entity_diagram", "anchor": "ed"},
                    {"id": "open_questions", "anchor": "oq"},
                    {"id": "related_threads", "anchor": "rt"},
                    {"id": "media_gallery", "anchor": "mg"},
                    {"id": "video_embed", "anchor": "ve"},
                    {"id": "pdf_preview", "anchor": "pp"},
                    {"id": "link_card", "anchor": "lc"},
                ],
                "media_pins": [
                    {"media_id": "m_inline_1", "fact_id": "fact_3", "slot": "inline"},
                ],
            },
            "tldr": "**JWT replaces SAML in the Acme Robotics SDK auth path with 24h TTL and in-memory session cache.**",
            "overview": "The Acme Robotics SDK migrated from SAML to JWT in Q1 2026, reducing auth latency and simplifying token validation. Production rollout begins March 1 with refresh-token strategy still open.",
            "body": (
                "<<MODULE:media_hero>>\n\n"
                "The migration was driven by three decisions:\n\n"
                "<<MODULE:key_facts>>\n\n"
                "<<MODULE:decision_log>>\n\n"
                "Auth flows touch these subsystems:\n\n"
                "<<MODULE:entity_diagram>>\n\n"
                "Open threads remain:\n\n"
                "<<MODULE:open_questions>>\n\n"
                "<<MODULE:related_threads>>\n\n"
                "<<MODULE:media_gallery>>\n\n"
                "<<MODULE:video_embed>>\n\n"
                "<<MODULE:pdf_preview>>\n\n"
                "<<MODULE:link_card>>"
            ),
        }
    )


# ---------------------------------------------------------------------------
# E2E pipeline tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_full_modular_pipeline_renders_all_module_types() -> None:
    """End-to-end: realistic cluster → planner → per-module renderers
    → marker substitution → assembled page. Verifies the entire
    pipeline produces a valid wiki page with content + structured
    module data persisted for the frontend."""
    call_count = {"n": 0}

    async def stub_llm(prompt: str) -> str:
        call_count["n"] += 1
        return _stub_llm_response()

    out = await compile_topic_page_modular(
        title="Acme Robotics SDK Auth Migration",
        summary="JWT replaces SAML in the SDK auth path",
        signals=_build_signals(),
        render_inputs=_build_render_inputs(),
        top_facts=[{"memory_text": f["memory_text"]} for f in _SYNTHETIC_FACTS[:3]],
        top_people=[{"name": "Alice"}, {"name": "Bob"}, {"name": "Charlie"}],
        date_range_start="2026-01-10",
        date_range_end="2026-03-05",
        llm=stub_llm,
    )

    # ── Cost contract: exactly ONE LLM call ──────────────────────────
    assert call_count["n"] == 1, (
        f"single-call architecture violated; LLM invoked {call_count['n']} times"
    )
    assert isinstance(out, ModularPageOutput)
    assert out.fell_back is False

    # ── Plan persistence ─────────────────────────────────────────────
    module_ids = [m["id"] for m in out.modules]
    # All 10 picked modules survived validation (signals satisfy each rule).
    assert "key_facts" in module_ids
    assert "decision_log" in module_ids
    assert "entity_diagram" in module_ids
    assert "open_questions" in module_ids
    assert "related_threads" in module_ids
    assert "media_hero" in module_ids
    assert "media_gallery" in module_ids
    assert "video_embed" in module_ids
    assert "pdf_preview" in module_ids
    assert "link_card" in module_ids
    # Media pin survived.
    assert len(out.media_pins) == 1
    assert out.media_pins[0].slot == "inline"

    # ── Content shape ────────────────────────────────────────────────
    assert out.content.startswith("**JWT replaces SAML")  # TL;DR first
    assert "Q1 2026" in out.content  # Overview prose
    assert "<<MODULE:" not in out.content  # all markers substituted
    # Compiler-rendered modules show up in the body.
    assert "Adopt JWT" in out.content  # decision_log row
    assert "How should refresh token rotation work?" in out.content  # open_questions
    assert "```mermaid" in out.content  # entity_diagram fence
    assert "Rate limiting strategy" in out.content  # related_threads link

    # ── Per-module data payloads (frontend dispatcher feed) ──────────
    by_id = {m["id"]: m for m in out.modules}
    # Markdown-emitting modules carry the rendered markdown.
    assert "Adopt JWT" in by_id["decision_log"]["data"]["markdown"]
    assert "renderer_kind" in by_id["decision_log"]["data"]
    # ``key_facts`` v2 is a frontend renderer — payload is a
    # structured items list, not markdown.
    kf = by_id["key_facts"]["data"]
    assert kf["renderer_kind"] == "frontend"
    assert isinstance(kf["items"], list)
    assert len(kf["items"]) >= 1
    # Media modules carry structured payloads, NOT markdown.
    hero = by_id["media_hero"]["data"]
    assert hero["renderer_kind"] == "frontend"
    assert hero["url"] == "https://example.com/auth-architecture.png"
    assert hero["alt"] == "Acme Robotics SDK auth architecture"
    assert hero["caption"].startswith("JWT replaces SAML")
    # Gallery has the unpinned screenshots only (excludes hero + inline).
    gallery = by_id["media_gallery"]["data"]
    gallery_urls = {item["url"] for item in gallery["items"]}
    assert len(gallery["items"]) == 3
    assert all("dashboard-screenshot" in u for u in gallery_urls)
    assert "https://example.com/auth-architecture.png" not in gallery_urls
    # PDF preview, video embed, link card each picked the right items.
    assert by_id["pdf_preview"]["data"]["items"][0]["url"].endswith(".pdf")
    assert by_id["video_embed"]["data"]["items"][0]["kind"] == "youtube"
    assert by_id["link_card"]["data"]["items"][0]["url"].startswith("https://example.org")


@pytest.mark.asyncio
async def test_e2e_validator_drops_ineligible_modules_silently() -> None:
    """Synthetic cluster has no events spanning ≥ 14 days and no
    alternatives → planner picks them anyway, validator drops them,
    page still renders with the eligible modules."""

    async def stub_llm(prompt: str) -> str:
        return json.dumps(
            {
                "plan": {
                    "modules": [
                        {"id": "key_facts", "anchor": "kf"},
                        {"id": "comparison_matrix", "anchor": "cm"},  # alts=0 → drop
                        {"id": "flow_chart", "anchor": "fc"},  # process_steps=0 → drop
                        {"id": "decision_log", "anchor": "dl"},
                    ]
                },
                "tldr": "**X.**",
                "overview": "Y.",
                "body": "<<MODULE:key_facts>>\n\n<<MODULE:decision_log>>",
            }
        )

    out = await compile_topic_page_modular(
        title="Test",
        summary="",
        signals=_build_signals(),
        render_inputs=_build_render_inputs(),
        top_facts=[],
        top_people=[],
        llm=stub_llm,
    )
    module_ids = [m["id"] for m in out.modules]
    assert "key_facts" in module_ids
    assert "decision_log" in module_ids
    assert "comparison_matrix" not in module_ids  # validator dropped
    assert "flow_chart" not in module_ids  # validator dropped


@pytest.mark.asyncio
async def test_e2e_no_private_data_in_module_payloads() -> None:
    """Privacy guard: assert NO real channel/user/URL data appears in
    the persisted module payloads. All values trace back to the
    synthetic fixtures defined at the top of this file."""

    async def stub_llm(prompt: str) -> str:
        return _stub_llm_response()

    out = await compile_topic_page_modular(
        title="Acme Robotics SDK Auth Migration",
        summary="",
        signals=_build_signals(),
        render_inputs=_build_render_inputs(),
        top_facts=[],
        top_people=[],
        llm=stub_llm,
    )
    serialized = json.dumps([out.content, out.modules], default=str)
    # Ban-list of substrings that would indicate real-data leakage.
    forbidden = [
        "tech-beever",
        "beever-atlas",
        "@gmail.com",
        "mattermost",
        "slack",
        "files.mattermost.com",
    ]
    lower = serialized.lower()
    for f in forbidden:
        assert f.lower() not in lower, (
            f"Privacy leak: forbidden substring '{f}' found in test output"
        )
