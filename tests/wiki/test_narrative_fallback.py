"""Tests for the narrative-article fallback paths in the orchestrator.

Spec: ``openspec/changes/wiki-narrative-articles/specs/wiki-narrative-articles/spec.md``
covers:

  - When the LLM response cannot be parsed as JSON, the orchestrator
    falls back to module-only rendering and logs
    ``narrative_article_fallback reason=parse_error``.
  - When citation coverage is below 80%, the validator rejects the
    payload and the orchestrator persists ``narrative_sections=[]``,
    logging ``narrative_article_fallback reason=low_citation_coverage``.
  - Both cases: page still renders (``ModularPageOutput.content`` is
    non-empty), and ``narrative_sections`` is empty so the frontend
    drops to module-only layout.
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
    cluster = {
        "title": "Authlib OIDC Adoption",
        # 6 facts so key_facts (≥ 5) qualifies and the plan does not
        # validate to empty (which would short-circuit to the
        # _fallback_output path before narrative state is attached).
        "member_facts": [
            {"fact_type": "decision", "author_name": "A", "date": "2026-04-01"},
            {"fact_type": "claim", "author_name": "B"},
            {"fact_type": "claim", "author_name": "C"},
            {"fact_type": "opinion", "author_name": "D"},
            {"fact_type": "claim", "author_name": "E"},
            {"fact_type": "event", "author_name": "F", "date": "2026-04-15"},
        ],
    }
    return compute_signals(
        cluster=cluster,
        decisions=[{"decision": "Adopt Authlib"}],
    )


def _render_inputs() -> dict:
    return {
        "facts": [
            {"memory_text": "Adopted Authlib for OIDC.", "fact_type": "decision", "importance": 8},
            {
                "memory_text": "Authlib supports OIDC discovery.",
                "fact_type": "claim",
                "importance": 7,
            },
            {"memory_text": "Replaces Authlib-free path.", "fact_type": "claim", "importance": 6},
            {
                "memory_text": "OIDC is a federation standard.",
                "fact_type": "claim",
                "importance": 5,
            },
            {"memory_text": "Migration completed Apr 2026.", "fact_type": "event", "importance": 6},
        ],
        "decisions": [
            {
                "decision": "Adopt Authlib",
                "status": "active",
                "made_by": "Alice",
                "date": "2026-04-15",
            },
        ],
        "page_id": "topic:authlib-oidc",
    }


# ---------------------------------------------------------------------------
# Parse-error fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_error_falls_back_to_module_only() -> None:
    """LLM returns non-JSON garbage → fallback fires; page still renders.

    The orchestrator logs ``narrative_article_fallback reason=parse_error``
    and the catastrophic ``_fallback_output`` path produces a key_facts-
    only page so the user sees something.
    """
    # Capture log records by attaching a list-handler to the orchestrator
    # logger directly. caplog doesn't reliably intercept loggers that
    # have custom handlers via the project's logging config.
    import logging

    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _ListHandler(level=logging.INFO)
    orch_logger = logging.getLogger("beever_atlas.wiki.modules.orchestrator")
    orch_logger.addHandler(handler)
    orch_logger.setLevel(logging.INFO)
    try:

        async def garbage_llm(prompt: str) -> str:
            return "not-actually-json {{{ broken"

        out = await compile_topic_page_modular(
            title="Authlib OIDC Adoption",
            summary="Auth migration.",
            signals=_signals_for_test(),
            render_inputs=_render_inputs(),
            top_facts=[],
            top_people=[],
            llm=garbage_llm,
        )
    finally:
        orch_logger.removeHandler(handler)

    assert isinstance(out, ModularPageOutput)
    # Fallback fired — output came from _fallback_output.
    assert out.fell_back is True
    # Narrative sections are empty so the frontend renders module-only.
    assert out.narrative_sections == []
    # Content is non-empty (page still renders).
    assert out.content
    # Structured fallback log line emitted.
    messages = [rec.getMessage() for rec in captured]
    assert any("narrative_article_fallback" in m and "parse_error" in m for m in messages), (
        f"expected parse_error fallback log; got {messages}"
    )


# ---------------------------------------------------------------------------
# Low-coverage fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_citation_coverage_rejects_narrative() -> None:
    """LLM returns valid JSON with mostly-uncited paragraphs → validator
    rejects → narrative_sections persisted as empty, page renders module-only."""
    import logging

    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _ListHandler(level=logging.INFO)
    orch_logger = logging.getLogger("beever_atlas.wiki.modules.orchestrator")
    orch_logger.addHandler(handler)
    orch_logger.setLevel(logging.INFO)

    # The validator drops uncited paragraphs BEFORE coverage gating —
    # so to trigger the low_citation_coverage gate we need the
    # validator to reject for a different reason. The
    # ``no_sections_after_validation`` path (every paragraph dropped)
    # tests the same fallback wiring with a stronger reject signal.
    response = {
        "plan": {
            "modules": [
                {"id": "key_facts", "anchor": "kf"},
            ]
        },
        "tldr": "**Authlib was adopted for OIDC.**",
        "overview": "The team migrated to Authlib in April 2026.",
        "narrative_sections": [
            {
                "anchor": "context",
                "heading": "Context",
                "paragraphs": [
                    # Both paragraphs uncited → validator drops both →
                    # no surviving paragraphs → no surviving section →
                    # validator returns rejected=True with reason
                    # ``no_sections_after_validation``.
                    {"text": "This is uncited prose.", "citations": [], "is_inference": False},
                    {"text": "More uncited prose.", "citations": [], "is_inference": False},
                ],
                "visual": None,
            },
        ],
        "body": "<<MODULE:key_facts>>",
    }

    async def fake_llm(prompt: str) -> str:
        return json.dumps(response)

    try:
        out = await compile_topic_page_modular(
            title="Authlib OIDC Adoption",
            summary="Auth migration.",
            signals=_signals_for_test(),
            render_inputs=_render_inputs(),
            top_facts=[],
            top_people=[],
            llm=fake_llm,
        )
    finally:
        orch_logger.removeHandler(handler)

    # Page still renders (modules survived).
    assert isinstance(out, ModularPageOutput)
    # Narrative sections empty — frontend renders module-only.
    assert out.narrative_sections == []
    # Telemetry surfaces the rejection reason.
    assert out.narrative_telemetry.get("rejected") is True
    # Structured fallback log emitted.
    messages = [rec.getMessage() for rec in captured]
    assert any("narrative_article_fallback" in m for m in messages), (
        f"expected at least one narrative_article_fallback log; got {messages}"
    )


# ---------------------------------------------------------------------------
# v3 prompt is always used (the WIKI_NARRATIVE_ARTICLES flag was removed —
# graceful fallback to module-only is the safety mechanism, not a flag).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v3_prompt_always_invoked() -> None:
    """The orchestrator always invokes the v3 prompt — there is no
    flag gate. When the LLM returns an empty narrative_sections array,
    the page still renders module-only because the validator yields
    an empty cleaned list and the ``narrative_article`` module's
    predicate fails naturally."""
    received_prompts: list[str] = []

    async def capturing_llm(prompt: str) -> str:
        received_prompts.append(prompt)
        # Return the minimum valid v3 response so parse succeeds.
        return json.dumps(
            {
                "plan": {"modules": [{"id": "key_facts", "anchor": "kf"}]},
                "tldr": "**X.**",
                "overview": "Y.",
                "narrative_sections": [],
                "body": "<<MODULE:key_facts>>",
            }
        )

    out = await compile_topic_page_modular(
        title="X",
        summary="Y",
        signals=_signals_for_test(),
        render_inputs=_render_inputs(),
        top_facts=[],
        top_people=[],
        llm=capturing_llm,
    )
    # The v3 prompt explicitly enumerates ``narrative_sections``; v2
    # does not. Use that as the path-discriminator.
    assert received_prompts, "expected at least one LLM call"
    assert '"narrative_sections":' in received_prompts[0], (
        "orchestrator did not invoke the v3 prompt"
    )
    # Empty narrative_sections + valid plan → page renders module-only.
    assert out.narrative_sections == []


# ---------------------------------------------------------------------------
# H-8: parse-error fallback emits narrative_article_metrics line
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_error_fallback_emits_metrics_line() -> None:
    """H-8: when the v3 path hits a parse error, the orchestrator emits
    BOTH a ``narrative_article_fallback`` line AND a
    ``narrative_article_metrics`` line so the soak dashboard can
    aggregate fallback rate consistently with the success path. Without
    the metrics line, dashboards see ``rejected=False, section_count=0``
    on every parse failure (because the metrics line was previously
    only emitted on success) and operators can't distinguish "flag
    OFF" from "v3 path crashed".
    """
    import logging

    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _ListHandler(level=logging.INFO)
    orch_logger = logging.getLogger("beever_atlas.wiki.modules.orchestrator")
    orch_logger.addHandler(handler)
    orch_logger.setLevel(logging.INFO)
    try:

        async def garbage_llm(prompt: str) -> str:
            return "definitely not json {{{"

        await compile_topic_page_modular(
            title="Authlib OIDC Adoption",
            summary="Auth migration.",
            signals=_signals_for_test(),
            render_inputs=_render_inputs(),
            top_facts=[],
            top_people=[],
            llm=garbage_llm,
        )
    finally:
        orch_logger.removeHandler(handler)

    messages = [rec.getMessage() for rec in captured]
    # Fallback log line is required.
    assert any("narrative_article_fallback" in m and "parse_error" in m for m in messages), (
        f"missing parse_error fallback log; got {messages}"
    )
    # Metrics log line is required (H-8).
    assert any(
        "narrative_article_metrics" in m
        and "parse_error" in m
        and "rejected=True" in m
        and "section_count=0" in m
        for m in messages
    ), f"missing narrative_article_metrics line on parse-error path; got {messages}"


@pytest.mark.asyncio
async def test_llm_error_fallback_emits_metrics_line() -> None:
    """H-8 sister case: when the LLM call itself raises, the
    orchestrator should still emit fallback + metrics lines so the
    dashboard can aggregate llm-error fallback rate."""
    import logging

    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _ListHandler(level=logging.INFO)
    orch_logger = logging.getLogger("beever_atlas.wiki.modules.orchestrator")
    orch_logger.addHandler(handler)
    orch_logger.setLevel(logging.INFO)
    try:

        async def crashing_llm(prompt: str) -> str:
            raise RuntimeError("simulated llm crash")

        await compile_topic_page_modular(
            title="Authlib OIDC Adoption",
            summary="Auth migration.",
            signals=_signals_for_test(),
            render_inputs=_render_inputs(),
            top_facts=[],
            top_people=[],
            llm=crashing_llm,
        )
    finally:
        orch_logger.removeHandler(handler)

    messages = [rec.getMessage() for rec in captured]
    assert any("narrative_article_fallback" in m and "llm_error" in m for m in messages), (
        f"missing llm_error fallback log; got {messages}"
    )
    assert any("narrative_article_metrics" in m and "llm_error" in m for m in messages), (
        f"missing metrics line on llm-error path; got {messages}"
    )
