"""Snapshot-style tests for ``MODULE_COMPILE_PROMPT_V3`` + builder.

Spec: ``openspec/changes/wiki-narrative-articles/specs/wiki-narrative-articles/spec.md``
covers the v3 prompt requirements:

  - Output schema includes ``narrative_sections`` array.
  - Per-section instructions (anchor, heading, paragraphs, citations,
    is_inference, optional visual).
  - Agent voice block (third-person, Wikipedia-editor, short
    paragraphs, NO activity narration).
  - Forbidden phrase list explicitly stated.
  - Word caps (150-400 per section, 1500-3000 typical article).
  - Worked examples (good + bad anti-pattern).
  - Single-pass — one LLM call returns plan + hero + narrative + body.
  - Archetype hint slot — Topic archetype gets empty hint; others
    inject a hint block (Session C wires this).
"""

from __future__ import annotations

from beever_atlas.wiki.prompts import (
    MODULE_COMPILE_PROMPT_V3,
    build_module_compile_prompt_v3,
    get_archetype_hint_block,
)


def _minimal_catalog() -> list[dict]:
    return [
        {
            "id": "hero_summary",
            "label": "Summary",
            "description": "Bold TL;DR + 2-3 sentence overview.",
            "rule": "ALWAYS pick when fact_count ≥ 1.",
        },
        {
            "id": "narrative_article",
            "label": "Article",
            "description": "Multi-section explanatory article.",
            "rule": "Pick when narrative_section_count ≥ 1.",
        },
    ]


# ---------------------------------------------------------------------------
# Schema-presence assertions
# ---------------------------------------------------------------------------


def test_v3_prompt_includes_narrative_sections_schema() -> None:
    """The output schema explicitly enumerates the narrative_sections array."""
    assert '"narrative_sections":' in MODULE_COMPILE_PROMPT_V3
    assert '"anchor":' in MODULE_COMPILE_PROMPT_V3
    assert '"heading":' in MODULE_COMPILE_PROMPT_V3
    assert '"paragraphs":' in MODULE_COMPILE_PROMPT_V3
    assert '"citations":' in MODULE_COMPILE_PROMPT_V3
    assert '"is_inference":' in MODULE_COMPILE_PROMPT_V3
    assert '"visual":' in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_states_word_caps() -> None:
    """Word caps are explicit so the LLM can self-regulate."""
    assert "150-400 words" in MODULE_COMPILE_PROMPT_V3
    assert "1,500-3,000" in MODULE_COMPILE_PROMPT_V3 or "1500-3000" in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_states_citation_discipline() -> None:
    """Citation discipline rules (HARD RULES) are spelled out."""
    assert "EVERY paragraph MUST cite at least one fact_id" in MODULE_COMPILE_PROMPT_V3
    assert "Inference paragraphs" in MODULE_COMPILE_PROMPT_V3
    # 80% coverage gate is mentioned.
    assert "80%" in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_lists_forbidden_phrases() -> None:
    """All six forbidden activity-narration phrases are listed."""
    for phrase in (
        "shared a link",
        "shared an article",
        "noted that",
        "mentioned that",
        "posted about",
        "presented that",
    ):
        assert phrase in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_lists_visual_kinds() -> None:
    """All six visual kinds are listed."""
    for kind in ("table", "mermaid", "list", "callout", "code", "blockquote"):
        assert kind in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_includes_worked_examples() -> None:
    """At least one GOOD + one BAD example anchors the discipline."""
    assert "GOOD" in MODULE_COMPILE_PROMPT_V3
    assert "BAD" in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_has_agent_voice_block() -> None:
    """Agent voice rules are stated."""
    assert "Third-person synthetic voice" in MODULE_COMPILE_PROMPT_V3
    assert "Wikipedia-editor" in MODULE_COMPILE_PROMPT_V3


def test_v3_prompt_has_single_pass_promise() -> None:
    """Output JSON-only contract reflects single-pass cardinality."""
    assert "Output JSON ONLY" in MODULE_COMPILE_PROMPT_V3


# ---------------------------------------------------------------------------
# Builder substitution + archetype hint slot
# ---------------------------------------------------------------------------


def test_builder_formats_with_template_variables() -> None:
    """``build_module_compile_prompt_v3`` produces a renderable string
    with all placeholders substituted."""
    prompt = build_module_compile_prompt_v3(
        signals={"fact_count": 12, "archetype": "topic"},
        module_catalog=_minimal_catalog(),
        title="Authlib OIDC Adoption",
        summary="The team adopted Authlib for OAuth/OIDC discovery.",
        top_facts=[{"fact_id": "f_1", "memory_text": "Adopted Authlib."}],
        top_people=[{"name": "Alice"}],
        date_range_start="2026-04-01",
        date_range_end="2026-05-01",
    )
    # No unsubstituted placeholders left over.
    assert "{module_catalog_block}" not in prompt
    assert "{signals_json}" not in prompt
    assert "{title}" not in prompt
    assert "{archetype_hint_block}" not in prompt
    # Page-level metadata + signals appear in the rendered prompt.
    assert "Authlib OIDC Adoption" in prompt
    assert "fact_count" in prompt
    assert "Adopted Authlib." in prompt


def test_topic_archetype_has_empty_hint_block() -> None:
    """Topic archetype passes an empty hint block — sections come from facts.

    Decision 2 in the design doc: the Topic archetype gets NO template
    hints (Session A defaults the slot to ``""``).
    """
    prompt = build_module_compile_prompt_v3(
        signals={"fact_count": 12, "archetype": "topic"},
        module_catalog=_minimal_catalog(),
        title="X",
        summary="Y",
        top_facts=[],
        top_people=[],
    )
    # The hint placeholder is replaced with the empty string. The
    # surrounding prompt structure (sections + module rules) is
    # still present.
    assert "## Module-selection rules" in prompt
    # No archetype-specific hint section appears for Topic — Session
    # C will populate this for Decision/Tension/Folder/Overview.


def test_archetype_hint_block_injects_when_provided() -> None:
    """Builder injects the caller-supplied hint block verbatim.

    Session C will pass real hint blocks for Decision / Tension /
    Folder / Overview archetypes. Session A just verifies the
    plumbing.
    """
    hint = (
        "## Decision archetype hint\n"
        "Decision pages typically have sections such as: Context, The "
        "decision, Why, Alternatives rejected, Implications, Open "
        "consequences. Use these IF the data supports them."
    )
    prompt = build_module_compile_prompt_v3(
        signals={"fact_count": 12, "archetype": "decision"},
        module_catalog=_minimal_catalog(),
        title="X",
        summary="Y",
        top_facts=[],
        top_people=[],
        archetype_hint_block=hint,
    )
    assert "Decision archetype hint" in prompt
    assert "Alternatives rejected" in prompt


def test_v3_prompt_structurally_distinct_from_v2() -> None:
    """v3 includes narrative_sections; v2 does not. Flag-OFF must use v2."""
    from beever_atlas.wiki.prompts import MODULE_COMPILE_PROMPT

    assert '"narrative_sections":' in MODULE_COMPILE_PROMPT_V3
    assert '"narrative_sections":' not in MODULE_COMPILE_PROMPT


# ---------------------------------------------------------------------------
# Archetype hint blocks (Phase 7 — Session C)
# ---------------------------------------------------------------------------
#
# Decision 2 in ``openspec/changes/wiki-narrative-articles/design.md``:
# Decision / Tension / Folder / Channel Overview archetypes receive
# *soft* section-structure hints; Topic archetype gets NO hint —
# sections come entirely from cluster content. Unknown archetypes
# return the empty string defensively.
# ---------------------------------------------------------------------------


def test_archetype_hint_block_decision() -> None:
    """Decision archetype hint enumerates Context/Why/Alternatives/Implications."""
    block = get_archetype_hint_block("decision")
    assert "Decision archetype" in block
    assert "Context" in block
    assert "Why" in block
    assert "Alternatives" in block
    assert "Implications" in block
    # Soft-hint discipline — block must explicitly tell the LLM to deviate.
    assert "DEVIATE" in block or "deviate" in block.lower()


def test_archetype_hint_block_tension() -> None:
    """Tension archetype hint frames balanced positions, not a winner."""
    block = get_archetype_hint_block("tension")
    assert "Position A" in block
    assert "Position B" in block
    assert "EQUAL weight" in block


def test_archetype_hint_block_folder() -> None:
    """Folder archetype hint stresses cross-cutting synthesis across children."""
    block = get_archetype_hint_block("folder")
    assert "Cross-cutting" in block
    assert "synthesize ACROSS" in block


def test_archetype_hint_block_channel_overview() -> None:
    """Channel overview archetype hint covers landmark sections + 5,000-word cap."""
    block = get_archetype_hint_block("channel_overview")
    assert "What is" in block
    assert "Architecture" in block
    assert "5,000 words" in block


def test_archetype_hint_block_overview_alias_matches_channel_overview() -> None:
    """``overview`` archetype value resolves to the same hint as ``channel_overview``."""
    assert get_archetype_hint_block("overview") == get_archetype_hint_block("channel_overview")


def test_archetype_hint_block_topic_returns_empty() -> None:
    """Topic archetype gets NO template — Decision 2 in the design doc."""
    assert get_archetype_hint_block("topic") == ""


def test_archetype_hint_block_unknown_returns_empty() -> None:
    """Defensive — unknown archetype returns empty string, never raises."""
    assert get_archetype_hint_block("nonexistent_archetype") == ""
    assert get_archetype_hint_block("") == ""


def test_archetype_hint_block_case_insensitive() -> None:
    """Archetype matching is case-insensitive — ``"Decision"`` and ``"DECISION"``
    return the same block as ``"decision"``."""
    base = get_archetype_hint_block("decision")
    assert get_archetype_hint_block("Decision") == base
    assert get_archetype_hint_block("DECISION") == base


def test_v3_prompt_with_decision_archetype_includes_decision_hint() -> None:
    """End-to-end — building a v3 prompt for a Decision-archetype page
    embeds the Decision hint block in the rendered output."""
    decision_hint = get_archetype_hint_block("decision")
    prompt = build_module_compile_prompt_v3(
        signals={"fact_count": 9, "archetype": "decision", "decision_count": 3},
        module_catalog=_minimal_catalog(),
        title="Adopting Authlib for OAuth",
        summary="The team picked Authlib over google-auth-oauthlib.",
        top_facts=[],
        top_people=[],
        archetype_hint_block=decision_hint,
    )
    # The Decision hint section appears verbatim in the rendered prompt.
    assert "Decision archetype" in prompt
    assert "Alternatives rejected" in prompt
    assert "Open consequences" in prompt


def test_v3_prompt_with_topic_archetype_omits_hint_section() -> None:
    """Topic archetype produces no hint section header in the rendered prompt."""
    topic_hint = get_archetype_hint_block("topic")
    assert topic_hint == ""
    prompt = build_module_compile_prompt_v3(
        signals={"fact_count": 12, "archetype": "topic"},
        module_catalog=_minimal_catalog(),
        title="OpenClaw Integration",
        summary="Pipeline integrates OpenClaw as a connector.",
        top_facts=[],
        top_people=[],
        archetype_hint_block=topic_hint,
    )
    # No archetype-specific section structure hint header is injected.
    assert "Section structure hint" not in prompt
    # The structural rest of the prompt is still present.
    assert "## Module-selection rules" in prompt
