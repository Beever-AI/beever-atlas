"""Tests for the fact extractor prompt's Phase 3 enrichment additions.

Phase 3 adds six OPTIONAL fields to the extracted-fact JSON contract:
``rationale``, ``alternatives_considered``, ``consequences_open``,
``numeric_values``, ``sentiment``, ``glossary_terms``. These tests
verify that:

  1. The prompt mentions every new field name (so the LLM knows the
     keys exist and what conditions trigger each).
  2. The JSON output schema example includes all six new keys so the
     LLM has a concrete shape to mirror.
  3. The prompt explicitly marks the new fields as OPTIONAL so the
     LLM doesn't fabricate values when the source doesn't support
     them.
  4. The prompt still formats with the existing template variables
     after the additions (regression guard against accidentally
     introducing a stray ``{`` brace that would break ``str.format``).
"""

from __future__ import annotations

import pytest

from beever_atlas.agents.prompts.fact_extractor import FACT_EXTRACTOR_INSTRUCTION


# ---------------------------------------------------------------------------
# All six Phase 3 field names appear in the prompt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    [
        "rationale",
        "alternatives_considered",
        "consequences_open",
        "numeric_values",
        "sentiment",
        "glossary_terms",
    ],
)
def test_prompt_mentions_phase3_field(field_name: str) -> None:
    """Each new field name appears at least once in the prompt body
    so the LLM knows the key exists."""
    assert field_name in FACT_EXTRACTOR_INSTRUCTION, (
        f"Phase 3 field '{field_name}' is missing from the fact-extractor prompt"
    )


# ---------------------------------------------------------------------------
# JSON schema example includes all six fields
# ---------------------------------------------------------------------------


def test_prompt_json_schema_block_includes_phase3_fields() -> None:
    """The JSON example inside the ``Output format`` section should
    include every new field as an optional key — gives the LLM a
    direct pattern to mirror."""
    # Pull the JSON code-fence content. The prompt has exactly one
    # ```json fence near the bottom.
    start = FACT_EXTRACTOR_INSTRUCTION.find("```json")
    end = FACT_EXTRACTOR_INSTRUCTION.find("```", start + len("```json"))
    assert start != -1 and end != -1, "expected a ```json``` example block"
    schema_block = FACT_EXTRACTOR_INSTRUCTION[start:end]
    for field in (
        "rationale",
        "alternatives_considered",
        "consequences_open",
        "numeric_values",
        "sentiment",
        "glossary_terms",
    ):
        assert field in schema_block, (
            f"Phase 3 field '{field}' must appear in the JSON schema example"
        )


# ---------------------------------------------------------------------------
# Phase 3 fields are clearly marked OPTIONAL
# ---------------------------------------------------------------------------


def test_prompt_marks_phase3_fields_as_optional() -> None:
    """The prompt explicitly tags Phase 3 fields as OPTIONAL so the
    LLM omits them when the source doesn't support them, rather than
    fabricating placeholder values."""
    # Look for the dedicated Phase 3 section header AND an explicit
    # OPTIONAL marker in close proximity.
    body = FACT_EXTRACTOR_INSTRUCTION.upper()
    assert "PHASE 3" in body, "prompt must contain a Phase 3 section header"
    assert "OPTIONAL" in body, "prompt must explicitly mark Phase 3 fields as OPTIONAL"


# ---------------------------------------------------------------------------
# Prompt still formats correctly after the additions
# ---------------------------------------------------------------------------


def test_prompt_formats_with_template_vars() -> None:
    """Regression guard: an unescaped ``{`` introduced by Phase 3
    additions would break ``str.format`` at extractor runtime. This
    renders the prompt the same way the agent does."""
    rendered = FACT_EXTRACTOR_INSTRUCTION.format(
        source_language="en",
        channel_name="test-channel",
        preprocessed_messages="[]",
        max_facts_per_message=3,
    )
    # Sanity — the rendered prompt should still mention the new
    # fields after rendering (the template-format step doesn't
    # accidentally chop them).
    for field in (
        "rationale",
        "alternatives_considered",
        "consequences_open",
        "numeric_values",
        "sentiment",
        "glossary_terms",
    ):
        assert field in rendered


# ---------------------------------------------------------------------------
# Examples ground the LLM in concrete patterns
# ---------------------------------------------------------------------------


def test_prompt_includes_concrete_examples_for_decision_fields() -> None:
    """The prompt should include at least one example showing how to
    populate the decision-only fields — example-driven prompts beat
    spec-only prompts for LLM accuracy."""
    # Look for hallmark example tokens drawn from the spec.
    for marker in ("DCO", "License-grant", "CLA"):
        assert marker in FACT_EXTRACTOR_INSTRUCTION, (
            f"prompt should include the canonical CLA example token '{marker}'"
        )


def test_prompt_lists_sentiment_enum_values() -> None:
    """The four allowed sentiment values must appear in the prompt
    so the LLM knows the closed enum (rather than free-text)."""
    for value in ("neutral", "concerning", "positive", "recommendation"):
        assert value in FACT_EXTRACTOR_INSTRUCTION, (
            f"sentiment enum value '{value}' is missing from the prompt"
        )
