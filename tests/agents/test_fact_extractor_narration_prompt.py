"""Tests for the fact-extractor prompt's narration discipline block.

Verifies that the synthesized-knowledge / activity-log block is present
in the prompt: forbidden phrases listed, good and bad rewrites shown,
and the author-attribution disambiguation guidance.

This is the prompt-side half of the defense-in-depth pair (the other
half is ``narration_filter.filter_facts`` running after extraction).
"""

from __future__ import annotations

import pytest

from beever_atlas.agents.prompts.fact_extractor import FACT_EXTRACTOR_INSTRUCTION


def test_prompt_contains_writing_style_synthesized_knowledge_block() -> None:
    """A clearly-titled block introduces the discipline."""
    assert "Writing style — synthesized knowledge, NOT activity log" in FACT_EXTRACTOR_INSTRUCTION


@pytest.mark.parametrize(
    "phrase",
    [
        "shared a link",
        "shared an article",
        "shared a [Neo4j blog] post",
        "shared a [GitHub] repository",
        "noted that",
        "mentioned that",
        "posted about",
        "presented that",
        "asked the team",
    ],
)
def test_prompt_lists_forbidden_phrase(phrase: str) -> None:
    """Each forbidden phrase is enumerated so the LLM sees the explicit
    set of constructs to avoid."""
    assert phrase in FACT_EXTRACTOR_INSTRUCTION, (
        f"Forbidden phrase '{phrase}' missing from fact-extractor prompt"
    )


def test_prompt_includes_good_examples() -> None:
    """The block shows positive examples of synthesized prose."""
    assert "GOOD examples" in FACT_EXTRACTOR_INSTRUCTION
    # At least one of the canonical good examples is present verbatim
    assert "Ory Hydra is an OAuth 2.0 + OpenID Connect provider" in FACT_EXTRACTOR_INSTRUCTION
    assert "fastapi-sso provides OAuth integration patterns" in FACT_EXTRACTOR_INSTRUCTION


def test_prompt_includes_bad_examples() -> None:
    """The block shows negative examples that get rejected/rewritten."""
    assert "BAD examples" in FACT_EXTRACTOR_INSTRUCTION
    assert "Thomas Chong shared a link to the GitHub repository for Ory Hydra" in (
        FACT_EXTRACTOR_INSTRUCTION
    )
    assert "Jacky Chan mentioned that fastapi-sso could be useful" in FACT_EXTRACTOR_INSTRUCTION


def test_prompt_disambiguates_author_field() -> None:
    """The block reminds the LLM that ``author_name`` carries the
    sharer attribution, so ``memory_text`` must not duplicate it."""
    assert "author_name" in FACT_EXTRACTOR_INSTRUCTION
    # The disambiguation sentence appears
    assert "preserved separately" in FACT_EXTRACTOR_INSTRUCTION


def test_prompt_demotion_guidance_for_unknown_resources() -> None:
    """When the link/resource is opaque, the LLM is told to demote
    importance to 'low' rather than fabricate context."""
    # Locate the demotion guidance — match flexibly on key tokens
    text = FACT_EXTRACTOR_INSTRUCTION
    assert "importance" in text and '"low"' in text
    assert "Do NOT fabricate" in text


def test_prompt_still_formats_with_template_variables() -> None:
    """Regression guard: adding the block must not break ``str.format``
    by introducing stray ``{`` braces. Render with all required vars."""
    rendered = FACT_EXTRACTOR_INSTRUCTION.format(
        source_language="en",
        channel_name="general",
        preprocessed_messages="[]",
        max_facts_per_message=3,
    )
    # Required variables expanded
    assert "{source_language}" not in rendered
    assert "{channel_name}" not in rendered
    assert "{preprocessed_messages}" not in rendered
    assert "{max_facts_per_message}" not in rendered
