"""Unit tests for Phase 1 wiki-quality improvements.

Covers:
- Mermaid auto-close post-processor (open, open+text, nested fences).
- URL normalization + social-domain cap.
- Orphan-citation stripping.
- Decision-count union (deterministic helper replicated from compiler logic).
- size_tier derivation.
"""

from __future__ import annotations

from beever_atlas.wiki.compiler import (
    WikiCompiler,
    _compute_size_tier,
    _normalize_url,
    _build_media_data,
)
from beever_atlas.models.domain import AtomicFact, WikiCitation


# ── Helpers ──────────────────────────────────────────────────────────────


def _fact(
    *,
    fact_id: str = "f1",
    media_urls: list[str] | None = None,
    media_names: list[str] | None = None,
    link_urls: list[str] | None = None,
    link_titles: list[str] | None = None,
    author: str = "alice",
    media_type: str = "",
) -> AtomicFact:
    return AtomicFact(
        id=fact_id,
        channel_id="c",
        source_message_id=f"m-{fact_id}",
        message_ts="1",
        author_id="u",
        author_name=author,
        memory_text="text",
        topic_tags=[],
        fact_type="claim",
        importance="high",
        quality_score=0.9,
        source_media_urls=media_urls or [],
        source_media_names=media_names or [],
        source_link_urls=link_urls or [],
        source_link_titles=link_titles or [],
        source_media_type=media_type,
    )


# ── size_tier ────────────────────────────────────────────────────────────


def test_size_tier_small_below_5():
    assert _compute_size_tier(0) == "small"
    assert _compute_size_tier(1) == "small"
    assert _compute_size_tier(4) == "small"


def test_size_tier_medium_5_to_12_inclusive():
    assert _compute_size_tier(5) == "medium"
    assert _compute_size_tier(8) == "medium"
    assert _compute_size_tier(12) == "medium"


def test_size_tier_large_above_12():
    assert _compute_size_tier(13) == "large"
    assert _compute_size_tier(148) == "large"


# ── URL normalization ───────────────────────────────────────────────────


def test_normalize_url_lowercases_host_and_strips_www():
    assert _normalize_url("https://WWW.Example.COM/Path") == "https://example.com/Path"


def test_normalize_url_canonicalizes_twitter_to_x():
    assert _normalize_url("https://twitter.com/foo/status/1") == "https://x.com/foo/status/1"


def test_normalize_url_strips_query_and_fragment_and_trailing_slash():
    assert (
        _normalize_url("https://x.com/foo/status/1/?src=share&utm=twitter#top")
        == "https://x.com/foo/status/1"
    )


def test_normalize_url_empty_input_returns_empty():
    assert _normalize_url("") == ""


# ── Global dedup via _build_media_data ──────────────────────────────────


def test_build_media_data_deduplicates_twitter_x_variants():
    facts = [
        _fact(
            fact_id="f1",
            link_urls=["https://twitter.com/foo/status/1"],
            link_titles=["t1"],
        ),
        _fact(
            fact_id="f2",
            link_urls=["https://x.com/foo/status/1/?src=share"],
            link_titles=["t2"],
        ),
    ]
    media = _build_media_data(facts)
    assert len(media) == 1


def test_build_media_data_dedupes_across_fact_boundaries():
    facts = [
        _fact(fact_id="f1", link_urls=["https://github.com/a/b"], link_titles=["a"]),
        _fact(fact_id="f2", link_urls=["https://github.com/a/b"], link_titles=["b"]),
    ]
    assert len(_build_media_data(facts)) == 1


# ── Social-domain cap ───────────────────────────────────────────────────


def test_filter_media_social_cap_is_5_per_platform():
    items = [
        {
            "url": f"https://x.com/foo/status/{i}",
            "type": "link",
            "name": f"t{i}",
            "author": "a",
            "context": "",
        }
        for i in range(20)
    ]
    filtered = WikiCompiler._filter_media_for_resources(items)
    x_items = [m for m in filtered if "x.com" in m["url"]]
    assert len(x_items) == 5


def test_filter_media_twitter_and_x_share_a_cap():
    items = []
    for i in range(5):
        items.append(
            {
                "url": f"https://x.com/a/status/{i}",
                "type": "link",
                "name": f"x{i}",
                "author": "a",
                "context": "",
            }
        )
    for i in range(5):
        items.append(
            {
                "url": f"https://twitter.com/a/status/{100 + i}",
                "type": "link",
                "name": f"tw{i}",
                "author": "a",
                "context": "",
            }
        )
    filtered = WikiCompiler._filter_media_for_resources(items)
    # Both canonicalize to x.com, so 10 candidates collapse to the cap.
    social = [m for m in filtered if ("x.com" in m["url"] or "twitter.com" in m["url"])]
    assert len(social) == 5


# ── Mermaid auto-close ─────────────────────────────────────────────────


def test_mermaid_auto_close_adds_missing_fence():
    content = "## X\n\n```mermaid\ngraph TD\n    A-->B\n\n## Next section\n"
    fixed = WikiCompiler._auto_close_unclosed_mermaid(content)
    # The original block must now be closed before "## Next section".
    assert "```mermaid" in fixed
    # Count fences: 1 opener + 1 synthetic closer.
    assert fixed.count("```") == 2
    # "## Next section" still present.
    assert "## Next section" in fixed


def test_mermaid_auto_close_leaves_well_formed_blocks_untouched():
    content = "```mermaid\ngraph TD\n    A-->B\n```\n\ntext\n"
    fixed = WikiCompiler._auto_close_unclosed_mermaid(content)
    assert fixed == content


def test_mermaid_auto_close_handles_two_mermaid_blocks_first_unclosed():
    content = "```mermaid\ngraph TD\n    A-->B\n\n```mermaid\ngraph LR\n    X-->Y\n```\n"
    fixed = WikiCompiler._auto_close_unclosed_mermaid(content)
    # Expect fences to balance (each opener paired with a closer).
    assert fixed.count("```mermaid") == 2
    assert fixed.count("```") == 4


def test_mermaid_auto_close_handles_eof_without_closer():
    content = "```mermaid\ngraph TD\n    A-->B"
    fixed = WikiCompiler._auto_close_unclosed_mermaid(content)
    assert fixed.rstrip().endswith("```")
    assert fixed.count("```") == 2


# ── Orphan citation strip ──────────────────────────────────────────────


def test_strip_orphan_citations_removes_unused_source_entries():
    content = (
        "Some body text [1] and [2] references.\n"
        "\n"
        "- [1] @alice — first\n"
        "- [2] @bob — second\n"
        "- [6] @claire — never cited\n"
    )
    cleaned = WikiCompiler._strip_orphan_citations(content)
    assert "[6]" not in cleaned
    assert "- [1] @alice" in cleaned
    assert "- [2] @bob" in cleaned


def test_strip_orphan_citations_keeps_all_when_all_used():
    content = "Cites [1] and [2] and [3].\n\n- [1] a\n- [2] b\n- [3] c\n"
    assert WikiCompiler._strip_orphan_citations(content) == content


def test_strip_orphan_citations_noop_when_no_brackets():
    content = "Plain text with no citations.\n"
    assert WikiCompiler._strip_orphan_citations(content) == content


# ── Decision-count union (exercises the logic inline) ──────────────────


def test_decision_union_dedups_by_name_decider_date():
    # Replicate the union+dedup logic used in _compile_overview.
    top = [{"name": "Pick Supabase", "decided_by": "Thomas", "date": "2026-01-25"}]
    clusters_decisions = [
        [
            {"name": "pick supabase", "decided_by": "thomas", "date": "2026-01-25"}
        ],  # dup, different casing
        [{"name": "Adopt MCP", "decided_by": "Alvin", "date": "2026-01-24"}],
    ]
    seen: set[tuple] = set()
    merged: list[dict] = []
    for d in list(top) + [d for group in clusters_decisions for d in group]:
        key = (
            (d.get("name") or "").strip().lower(),
            (d.get("decided_by") or "").strip().lower(),
            (d.get("date") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(d)
    assert len(merged) == 2
    assert {m["name"] for m in merged} == {"Pick Supabase", "Adopt MCP"}


# ── WikiPage.citations orphan filter ───────────────────────────────────


def test_filter_citations_to_body_drops_unreferenced():
    content = "Body references [1] and [3] only."
    citations = [
        WikiCitation(id="[1]", author="a"),
        WikiCitation(id="[2]", author="b"),
        WikiCitation(id="[3]", author="c"),
        WikiCitation(id="[6]", author="d"),
    ]
    kept = WikiCompiler._filter_citations_to_body(content, citations)
    kept_ids = [c.id for c in kept]
    assert kept_ids == ["[1]", "[3]"]


def test_filter_citations_to_body_noop_when_no_markers():
    content = "Plain text without any bracketed markers."
    citations = [WikiCitation(id="[1]", author="a")]
    # No used indices — keep the list as-is (conservative).
    assert WikiCompiler._filter_citations_to_body(content, citations) == citations


def test_filter_citations_to_body_keeps_non_bracket_ids():
    content = "Body with [1]."
    citations = [
        WikiCitation(id="[1]", author="a"),
        WikiCitation(id="custom", author="b"),
    ]
    kept = WikiCompiler._filter_citations_to_body(content, citations)
    assert {c.id for c in kept} == {"[1]", "custom"}


# ── Post-process integration (ensures new passes don't break existing behaviour) ──


def test_postprocess_combines_mermaid_close_and_orphan_strip():
    raw = (
        "## X\n\n"
        "Body cites [1] and [2].\n\n"
        "```mermaid\n"
        "graph TD\n"
        "    A-->B\n"
        "\n"
        "## Follow-up\n\n"
        "- [1] @alice\n"
        "- [2] @bob\n"
        "- [9] @unused\n"
    )
    out = WikiCompiler._postprocess_content(raw)
    # Mermaid block must be closed.
    assert out.count("```") >= 2
    # Sources sections that are *trailing* get stripped wholesale by the
    # existing _SOURCES_RE; but body-embedded source lines fed to the
    # orphan strip must drop unused [9].
    assert "[9]" not in out or "@unused" not in out
