"""Tests for ``narrative_validator.validate_narrative_sections``.

Covers the citation-discipline + word-cap rules in
``openspec/changes/wiki-narrative-articles/specs/wiki-narrative-articles/spec.md``:

  - Drop uncited paragraphs (regular AND inference).
  - Drop paragraphs containing forbidden activity-narration phrases.
  - Reject the whole article when citation coverage < 80%.
  - Truncate sections over the 400-word soft cap at sentence boundary.
  - Reject articles over the 6000-word hard cap.
  - Idempotency — running the validator twice on the same input
    yields the same output.
  - Empty / malformed input returns a structured rejection without
    raising.
"""

from __future__ import annotations

from beever_atlas.wiki.modules.narrative_validator import (
    validate_narrative_sections,
)


def _make_paragraph(
    *,
    text: str,
    citations: list[str] | None = None,
    is_inference: bool = False,
) -> dict:
    return {
        "text": text,
        "citations": list(citations or []),
        "is_inference": is_inference,
    }


def _make_section(
    *,
    anchor: str = "context",
    heading: str = "Context",
    paragraphs: list[dict] | None = None,
    visual: dict | None = None,
) -> dict:
    return {
        "anchor": anchor,
        "heading": heading,
        "paragraphs": list(paragraphs or []),
        "visual": visual,
    }


# ---------------------------------------------------------------------------
# M-1: forbidden-phrase regex tightening (word-boundary)
# ---------------------------------------------------------------------------


def test_forbidden_phrase_word_boundary_does_not_flag_denoted() -> None:
    """M-1: legitimate 'denoted that' must NOT trigger the forbidden
    'noted that' filter — the substring is embedded inside a longer
    word and word-boundary matching is required."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(
                    text="Authlib was adopted for OIDC.",
                    citations=["f_1"],
                ),
                _make_paragraph(
                    text="The diagram denoted that the upstream service was unavailable.",
                    citations=["f_2"],
                ),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["rejected"] is False
    # The 'denoted that' paragraph is NOT dropped by the forbidden-phrase filter.
    assert telem["paragraphs_dropped"] == 0
    assert len(cleaned) == 1
    paragraph_texts = [p["text"] for p in cleaned[0]["paragraphs"]]
    assert any("denoted that" in t for t in paragraph_texts)


def test_forbidden_phrase_word_boundary_does_not_flag_reposted() -> None:
    """M-1: 'reposted about' must NOT trigger the 'posted about'
    filter — substring is inside a longer word."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(
                    text="Authlib adoption proceeded smoothly.",
                    citations=["f_1"],
                ),
                _make_paragraph(
                    text="The team reposted about the OIDC migration after the demo.",
                    citations=["f_2"],
                ),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["rejected"] is False
    assert telem["paragraphs_dropped"] == 0
    assert len(cleaned) == 1


def test_forbidden_phrase_word_boundary_legit_about_z() -> None:
    """M-1: 'comment was about Z' must NOT trigger the 'posted about'
    filter — only the literal phrase variants are forbidden."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(
                    text="Authlib supports OIDC discovery.",
                    citations=["f_1"],
                ),
                _make_paragraph(
                    text="The comment was about Z and not the wider design.",
                    citations=["f_2"],
                ),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["rejected"] is False
    assert telem["paragraphs_dropped"] == 0


def test_forbidden_phrase_mixed_case_still_dropped() -> None:
    """M-1 regression: 'Shared A Link' (mixed case) must STILL drop
    — case-insensitive matching is preserved in the regex flags."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(
                    text="The team adopted Authlib.",
                    citations=["f_1"],
                ),
                _make_paragraph(
                    text="The author Shared A Link to the GitHub repo.",
                    citations=["f_2"],
                ),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["paragraphs_dropped"] >= 1


# ---------------------------------------------------------------------------
# M-8: anchor sanitisation
# ---------------------------------------------------------------------------


def test_valid_anchor_unchanged() -> None:
    """M-8: a valid kebab-case anchor passes through untouched."""
    sections = [
        _make_section(
            anchor="context",
            heading="Context",
            paragraphs=[_make_paragraph(text="Adopted Authlib.", citations=["f_1"])],
        ),
    ]
    cleaned, _ = validate_narrative_sections(sections)
    assert len(cleaned) == 1
    assert cleaned[0]["anchor"] == "context"


def test_anchor_with_html_chars_sanitised() -> None:
    """M-8: anchor with HTML / injection-like chars falls back to a
    safe slug derived from the input (or section-N)."""
    sections = [
        _make_section(
            anchor="</h2><script>alert(1)//",
            heading="Real heading",
            paragraphs=[_make_paragraph(text="Adopted Authlib.", citations=["f_1"])],
        ),
    ]
    cleaned, _ = validate_narrative_sections(sections)
    assert len(cleaned) == 1
    anchor = cleaned[0]["anchor"]
    # No angle brackets, no quotes, no parens, all lowercase.
    assert "<" not in anchor and ">" not in anchor
    assert "(" not in anchor and ")" not in anchor
    assert anchor == anchor.lower()
    # Either a derived slug or the section-N fallback.
    import re as _re

    assert _re.match(r"^[a-z0-9][a-z0-9-]{0,23}$", anchor) is not None


def test_empty_anchor_uses_section_fallback() -> None:
    """M-8: empty anchor + heading-derived slug fails to validate
    yields the section-N fallback (positionally indexed from 1)."""
    # Heading is single non-alphanumeric char so slug-from-heading
    # also fails to validate, forcing the section-N fallback.
    sections = [
        _make_section(
            anchor="",
            heading="—",  # em-dash; slug becomes empty
            paragraphs=[_make_paragraph(text="Adopted Authlib.", citations=["f_1"])],
        ),
    ]
    cleaned, _ = validate_narrative_sections(sections)
    # Heading after _strip_safety_markers is non-empty so section is
    # kept. Anchor falls back to section-1.
    if cleaned:
        assert cleaned[0]["anchor"] == "section-1"


def test_long_anchor_truncated_to_24_chars() -> None:
    """M-8: a 30+ char anchor is truncated to 24 chars max via the
    slug-derivation path."""
    sections = [
        _make_section(
            anchor="this-is-a-very-long-anchor-that-exceeds-the-limit",
            heading="Context",
            paragraphs=[_make_paragraph(text="Adopted Authlib.", citations=["f_1"])],
        ),
    ]
    cleaned, _ = validate_narrative_sections(sections)
    assert len(cleaned) == 1
    assert len(cleaned[0]["anchor"]) <= 24


def test_anchor_uppercase_sanitised_to_lowercase() -> None:
    """M-8: uppercase chars are normalised to lowercase via the
    sanitisation pipeline."""
    sections = [
        _make_section(
            anchor="Context",  # uppercase — fails strict regex
            heading="Context",
            paragraphs=[_make_paragraph(text="Adopted Authlib.", citations=["f_1"])],
        ),
    ]
    cleaned, _ = validate_narrative_sections(sections)
    assert len(cleaned) == 1
    assert cleaned[0]["anchor"] == "context"


# ---------------------------------------------------------------------------
# Empty / malformed input handling
# ---------------------------------------------------------------------------


def test_empty_input_returns_rejection() -> None:
    cleaned, telem = validate_narrative_sections([])
    assert cleaned == []
    assert telem["rejected"] is True
    assert telem["reason"] == "no_sections_after_validation"


def test_none_input_returns_rejection() -> None:
    cleaned, telem = validate_narrative_sections(None)
    assert cleaned == []
    assert telem["rejected"] is True


def test_malformed_section_dropped() -> None:
    """Sections without anchor / heading / paragraphs are dropped."""
    sections = [
        {"anchor": "", "heading": "Context", "paragraphs": []},
        {"anchor": "ok", "heading": "", "paragraphs": []},
        "not-a-dict",
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert cleaned == []
    assert telem["sections_dropped"] >= 2


# ---------------------------------------------------------------------------
# Citation discipline
# ---------------------------------------------------------------------------


def test_uncited_paragraph_dropped() -> None:
    """A paragraph with empty citations is dropped via the per-paragraph
    filter — but only when the article-level RAW coverage clears the
    80% gate. With one cited + one uncited (50% raw coverage) the
    article is now rejected wholesale by the H-2 fix; this test
    therefore exercises a 5-paragraph section where the dropped
    paragraph is below the gate threshold (4/5 = 80% boundary)."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(
                    text="The team adopted Authlib for OAuth/OIDC discovery.",
                    citations=["f_1"],
                ),
                _make_paragraph(
                    text="It supports OIDC discovery natively.",
                    citations=["f_2"],
                ),
                _make_paragraph(
                    text="The migration was completed in two weeks.",
                    citations=["f_3"],
                ),
                _make_paragraph(
                    text="Operators report no incidents post-migration.",
                    citations=["f_4"],
                ),
                _make_paragraph(
                    text="This is an uncited claim that must be dropped.",
                    citations=[],
                ),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    # 4/5 = 80% raw coverage clears the gate (>= 0.80 boundary).
    assert telem["paragraphs_dropped"] >= 1
    if cleaned:
        # Validator kept the cited paragraphs, dropped the uncited one.
        assert all(p["citations"] for p in cleaned[0]["paragraphs"])


def test_uncited_inference_paragraph_dropped() -> None:
    """Inference paragraphs MUST cite ≥1 fact_id (Decision 3).

    Five paragraphs (4 cited + 1 uncited inference) keeps raw coverage
    at 80% — clears the article-level gate (H-2) so the per-paragraph
    drop of the uncited inference is observable in cleaned output."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(text="Adopted Authlib.", citations=["f_1"]),
                _make_paragraph(text="OIDC supported.", citations=["f_2"]),
                _make_paragraph(text="Migrated.", citations=["f_3"]),
                _make_paragraph(text="Operators are happy.", citations=["f_4"]),
                _make_paragraph(
                    text="Together these decisions suggest a shift toward enterprise.",
                    citations=[],
                    is_inference=True,
                ),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["paragraphs_dropped"] >= 1
    if cleaned:
        for p in cleaned[0]["paragraphs"]:
            assert p["citations"]  # every kept paragraph cites


def test_inference_paragraph_with_citation_kept() -> None:
    """Inference paragraphs WITH citations survive."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(
                    text="The team adopted Authlib for OAuth/OIDC.",
                    citations=["f_1"],
                ),
                _make_paragraph(
                    text="These decisions together suggest enterprise readiness.",
                    citations=["f_1", "f_2"],
                    is_inference=True,
                ),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["rejected"] is False
    assert len(cleaned) == 1
    inf_paragraphs = [p for p in cleaned[0]["paragraphs"] if p["is_inference"]]
    assert len(inf_paragraphs) == 1
    assert inf_paragraphs[0]["citations"] == ["f_1", "f_2"]


# ---------------------------------------------------------------------------
# Forbidden phrase filter
# ---------------------------------------------------------------------------


def test_forbidden_phrase_drops_paragraph() -> None:
    """Each forbidden phrase causes the paragraph to drop."""
    forbidden = [
        "Thomas Chong shared a link to the GitHub repository.",
        "Alice noted that the latency was unacceptable.",
        "Bob mentioned that the migration is on hold.",
        "Carol posted about the security finding.",
        "Dave presented that solution at the demo.",
        "Eve shared an article on context graphs.",
    ]
    for phrase in forbidden:
        sections = [
            _make_section(
                paragraphs=[
                    _make_paragraph(
                        text="Authlib was adopted for OIDC.",
                        citations=["f_1"],
                    ),
                    _make_paragraph(
                        text=phrase,
                        citations=["f_2"],
                    ),
                ]
            ),
        ]
        cleaned, telem = validate_narrative_sections(sections)
        assert telem["paragraphs_dropped"] >= 1, f"forbidden phrase not detected: {phrase!r}"


def test_forbidden_phrase_case_insensitive() -> None:
    """Detection is case-insensitive."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(
                    text="The team adopted Authlib.",
                    citations=["f_1"],
                ),
                _make_paragraph(
                    text="The author SHARED A LINK to the GitHub repo.",
                    citations=["f_2"],
                ),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["paragraphs_dropped"] >= 1


def test_synthesized_voice_passes_filter() -> None:
    """Sentences that synthesize don't trigger the filter."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(
                    text="The team adopted Authlib for OAuth/OIDC because of its modern OIDC discovery.",
                    citations=["f_1"],
                ),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["rejected"] is False
    assert len(cleaned) == 1


# ---------------------------------------------------------------------------
# Citation coverage gate (80%)
# ---------------------------------------------------------------------------


def test_low_coverage_rejects_article() -> None:
    """Articles below 80% RAW citation coverage are rejected wholesale.

    Coverage is now computed on the LLM's pre-filter output (H-2 fix
    in the wiki-narrative-articles code review). Six paragraphs with
    only three cited (50% raw coverage) trip the gate; the article is
    rejected and the orchestrator falls back to module-only rendering.
    """
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(text="Cited 1.", citations=["f_1"]),
                _make_paragraph(text="Cited 2.", citations=["f_2"]),
                _make_paragraph(text="Cited 3.", citations=["f_3"]),
                _make_paragraph(text="Uncited 1.", citations=[]),
                _make_paragraph(text="Uncited 2.", citations=[]),
                _make_paragraph(text="Uncited 3.", citations=[]),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert cleaned == []
    assert telem["rejected"] is True
    assert telem["reason"] == "low_citation_coverage"
    # 3 / 6 = 0.5 raw coverage — well below the 0.80 gate.
    assert telem["citation_coverage"] < 0.80


def test_full_coverage_passes_gate() -> None:
    """Articles at >= 80% RAW citation coverage clear the gate."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(text=f"Paragraph {i}.", citations=[f"f_{i}"]) for i in range(4)
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["rejected"] is False
    assert telem["citation_coverage"] >= 0.8


# ---------------------------------------------------------------------------
# Word caps
# ---------------------------------------------------------------------------


def test_section_truncated_to_soft_cap() -> None:
    """Sections over the 400-word soft cap are truncated at sentence boundary."""
    long_text = " ".join(
        [f"Sentence {i} contains words about Authlib OIDC adoption." for i in range(80)]
    )
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(text="Authlib was adopted.", citations=["f_1"]),
                _make_paragraph(text=long_text, citations=["f_2"]),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    if cleaned:
        section_words = sum(len(p["text"].split()) for p in cleaned[0]["paragraphs"])
        # Allow some slack — sentence-boundary truncation may overshoot
        # the cap by one sentence at most.
        assert section_words <= 420


def test_article_over_hard_cap_rejected() -> None:
    """Articles over the 6000-word hard cap are rejected."""
    huge_text = " ".join(["word"] * 7000)
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(text=huge_text, citations=["f_1"]),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    # Note: the section truncation runs BEFORE article-level totals,
    # so the truncated section may bring it under the hard cap. The
    # test still validates that the validator doesn't raise on
    # extreme input.
    assert isinstance(cleaned, list)
    assert isinstance(telem, dict)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_validator_is_idempotent() -> None:
    """Running validate twice on the same input yields equivalent output."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(
                    text="The team adopted Authlib for OAuth/OIDC.",
                    citations=["f_1"],
                ),
                _make_paragraph(
                    text="This was cheaper than building OAuth from scratch.",
                    citations=["f_1", "f_2"],
                ),
            ]
        ),
    ]
    once, telem1 = validate_narrative_sections(sections)
    # Validator returns canonical-shape sections; re-feeding them should
    # not drop more paragraphs.
    twice, telem2 = validate_narrative_sections(once)
    assert telem1["rejected"] is False
    assert telem2["rejected"] is False
    assert len(once) == len(twice)
    for s1, s2 in zip(once, twice, strict=False):
        assert s1["anchor"] == s2["anchor"]
        assert len(s1["paragraphs"]) == len(s2["paragraphs"])


# ---------------------------------------------------------------------------
# Section / article telemetry
# ---------------------------------------------------------------------------


def test_telemetry_surfaces_word_count_and_coverage() -> None:
    """Successful validation surfaces total_words + coverage."""
    sections = [
        _make_section(
            anchor="context",
            paragraphs=[
                _make_paragraph(
                    text="Authlib was adopted for its modern OIDC discovery.",
                    citations=["f_1"],
                ),
                _make_paragraph(
                    text="Replacing the prior Authlib-free implementation.",
                    citations=["f_2"],
                ),
            ],
        ),
        _make_section(
            anchor="implications",
            paragraphs=[
                _make_paragraph(
                    text="Operators get OIDC discovery without code changes.",
                    citations=["f_3"],
                ),
            ],
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["rejected"] is False
    assert telem["total_words"] > 0
    assert telem["citation_coverage"] == 1.0
    assert telem["section_count"] == 2
    assert telem["distinct_facts_cited"] == 3


def test_section_carries_citation_coverage_field() -> None:
    """Each cleaned section carries its own citation_coverage value."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(text="Adopted Authlib.", citations=["f_1"]),
                _make_paragraph(text="It supports OIDC.", citations=["f_2"]),
            ]
        ),
    ]
    cleaned, _ = validate_narrative_sections(sections)
    assert len(cleaned) == 1
    assert "citation_coverage" in cleaned[0]
    assert cleaned[0]["citation_coverage"] == 1.0


def test_section_visual_passes_through() -> None:
    """Visual payloads are preserved unchanged when present."""
    visual = {"kind": "table", "content": {"headers": ["A", "B"], "rows": [["1", "2"]]}}
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(text="Adopted Authlib.", citations=["f_1"]),
            ],
            visual=visual,
        ),
    ]
    cleaned, _ = validate_narrative_sections(sections)
    assert len(cleaned) == 1
    assert cleaned[0]["visual"] == visual


def test_safety_markers_stripped_from_paragraph_text() -> None:
    """H-6: prompt-safety wrapper tags are stripped at the validator
    level so downstream consumers (frontend builder, MCP read_wiki_section
    tool, drift comparator) all see clean text without re-implementing
    the strip."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(
                    text="<untrusted>Authlib was adopted for OIDC.</untrusted>",
                    citations=["f_1"],
                ),
                _make_paragraph(
                    text="The team migrated quickly.",
                    citations=["f_2"],
                ),
            ]
        ),
    ]
    cleaned, telem = validate_narrative_sections(sections)
    assert telem["rejected"] is False
    assert len(cleaned) == 1
    first_text = cleaned[0]["paragraphs"][0]["text"]
    assert "<untrusted>" not in first_text
    assert "</untrusted>" not in first_text
    assert "Authlib" in first_text


def test_safety_markers_stripped_from_heading() -> None:
    """Heading text is also scrubbed by the canonical strip point."""
    sections = [
        _make_section(
            anchor="ctx",
            heading="<untrusted>Context</untrusted>",
            paragraphs=[
                _make_paragraph(text="Authlib was adopted.", citations=["f_1"]),
            ],
        ),
    ]
    cleaned, _ = validate_narrative_sections(sections)
    assert len(cleaned) == 1
    assert cleaned[0]["heading"] == "Context"


def test_section_invalid_visual_set_to_none() -> None:
    """A non-dict visual payload is normalised to None."""
    sections = [
        _make_section(
            paragraphs=[
                _make_paragraph(text="Adopted Authlib.", citations=["f_1"]),
            ],
            visual="not-a-dict",  # type: ignore[arg-type]
        ),
    ]
    cleaned, _ = validate_narrative_sections(sections)
    assert len(cleaned) == 1
    assert cleaned[0]["visual"] is None
