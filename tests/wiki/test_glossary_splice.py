"""Tests for deterministic Glossary section splicing.

Glossary generation sometimes emits only the relationship mermaid diagram
and drops the Terms table + Introduction. The splicer fills those gaps so
readers always see definitions, not just a diagram.
"""

from types import SimpleNamespace

import pytest

from beever_atlas.wiki.compiler import (
    _build_compiled_topic_slug_index,
    _collect_glossary_entries,
    _render_glossary_terms_table,
    _resolve_topic_compile_threshold,
    _rewrite_topic_wikilinks,
    _slugify,
    _splice_glossary_sections,
    _topic_slug_for_title,
)

# Back-compat alias so existing test bodies keep working
_rewrite_glossary_wikilinks = _rewrite_topic_wikilinks


def _cluster(title, entities):
    return SimpleNamespace(title=title, key_entities=entities, key_facts=[])


def test_collect_merges_dict_terms_with_cluster_entities():
    glossary = [
        {
            "term": "Beever",
            "definition": "Memory system",
            "first_mentioned_by": "Alice",
            "related_topics": ["Arch"],
        },
        "MCP",
    ]
    clusters = [
        _cluster("Arch", [{"name": "Beever", "description": "extra"}]),
        _cluster("Integration", [{"name": "MCP", "description": "Protocol"}]),
    ]
    rows = _collect_glossary_entries(glossary, clusters)
    names = [r["term"] for r in rows]
    assert names == ["Beever", "MCP"]
    beever = next(r for r in rows if r["term"] == "Beever")
    assert beever["definition"] == "Memory system"  # dict definition wins
    assert "Arch" in beever["related_topics"]
    mcp = next(r for r in rows if r["term"] == "MCP")
    assert mcp["definition"] == "Protocol"
    assert "Integration" in mcp["related_topics"]


def test_render_terms_table_escapes_pipes_and_falls_back():
    rows = [
        {"term": "X|Y", "definition": "", "first_mentioned_by": "", "related_topics": []},
    ]
    table = _render_glossary_terms_table(rows)
    assert "X\\|Y" in table
    assert "Referenced in this channel." in table
    assert "| — |" in table  # first_mentioned_by fallback


def test_splice_adds_both_missing_sections():
    diagram_only = "## Relationship diagram\n```mermaid\ngraph TD\nA-->B\n```\n"
    glossary = [
        {
            "term": "Beever",
            "definition": "Memory sys",
            "first_mentioned_by": "Alice",
            "related_topics": ["Arch"],
        }
    ]
    clusters = []
    out = _splice_glossary_sections(diagram_only, glossary, clusters)
    assert "## Introduction" in out
    assert "## Terms" in out
    assert "| Beever | Memory sys | Alice | Arch |" in out


def test_splice_respects_existing_sections():
    existing = (
        "## Relationship diagram\n```mermaid\ngraph TD\nA-->B\n```\n\n"
        "## Introduction\n\nHello.\n\n"
        "## Terms\n\n| T | D | — | — |\n|---|---|---|---|\n| X | y | — | — |\n"
    )
    out = _splice_glossary_sections(existing, [{"term": "X"}], [])
    assert out.count("## Introduction") == 1
    assert out.count("## Terms") == 1


def test_splice_noop_on_empty_content():
    assert _splice_glossary_sections("", [], []) == ""


def test_splice_after_postprocess_closes_mermaid_first():
    """Regression: when the LLM returns an unclosed ```mermaid block, the
    auto-closer in _postprocess_content must run BEFORE the splice — otherwise
    the appended Introduction + Terms end up INSIDE the unclosed mermaid block
    and render as a mermaid syntax error."""
    from beever_atlas.wiki.compiler import WikiCompiler

    raw = "## Relationship Diagram\n```mermaid\ngraph TD\nA-->B\n"  # no closing ```
    closed = WikiCompiler._postprocess_content(raw)
    # The auto-closer must have sealed the mermaid block with a lone ``` line.
    assert closed.rstrip().endswith("```")
    # Splicing onto the sealed content appends sections *after* the close.
    out = _splice_glossary_sections(
        closed,
        [
            {
                "term": "Beever",
                "definition": "Memory",
                "first_mentioned_by": "Alice",
                "related_topics": [],
            }
        ],
        [],
    )
    assert "## Introduction" in out
    assert "## Terms" in out
    # The closing ``` must appear BEFORE "## Introduction" — proving the
    # Introduction is outside the mermaid block.
    assert out.index("```", out.index("```mermaid") + 1) < out.index("## Introduction")


def test_splice_skips_terms_when_no_data():
    diagram_only = "## Relationship diagram\n```mermaid\ngraph TD\nA-->B\n```\n"
    out = _splice_glossary_sections(diagram_only, [], [])
    # Introduction always rendered, Terms skipped when no entries
    assert "## Introduction" in out
    assert "## Terms" not in out


# ── Part 1: Tiered topic-compile threshold ──────────────────────────────


@pytest.mark.parametrize(
    "cluster_count, expected_min_facts",
    [
        # < 4 clusters → 1 fact suffices (very sparse channel)
        (0, 1),
        (1, 1),
        (3, 1),
        # 4-7 clusters → 2 facts
        (4, 2),
        (5, 2),
        (7, 2),
        # 8-15 clusters → 3 facts (current behaviour)
        (8, 3),
        (12, 3),
        (15, 3),
        # 16+ clusters → 3 facts (unchanged for dense channels)
        (16, 3),
        (50, 3),
        (1000, 3),
    ],
)
def test_resolve_topic_compile_threshold_tiers(cluster_count, expected_min_facts):
    """Tiered policy must produce the documented min-facts threshold for
    every cluster-count band so sparse channels stop dropping every topic."""
    assert _resolve_topic_compile_threshold(cluster_count) == expected_min_facts


# ── Part 2: Glossary wikilink slug matches topic-page slug ──────────────


def test_topic_slug_helper_matches_compiler_slugify():
    """The Glossary's wikilink rewriter and the topic-page compiler MUST
    derive the same slug from the same title — otherwise the link points
    at a non-existent page even though the page was compiled."""
    title = "Team Discussion on Work From Home Amidst Safety Concerns"
    # The compiler's topic-page slug assignment uses bare ``_slugify(title)``;
    # the rewriter goes through ``_topic_slug_for_title``. Both must agree.
    assert _topic_slug_for_title(title) == _slugify(title)


def test_rewrite_wikilinks_to_compiled_topics_emits_markdown_link():
    title = "Team Discussion on Work From Home Amidst Safety Concerns"
    content = f"Related: [[{title}]] and an unknown [[Some Skipped Topic]]."
    out = _rewrite_glossary_wikilinks(content, [title])
    slug = _slugify(title)
    # Compiled topic resolves to a real markdown link with the canonical slug.
    assert f"[{title}](/wiki/{slug})" in out
    # Skipped topic falls back to plain text — no red broken-link surface.
    assert "[[Some Skipped Topic]]" not in out
    assert "Some Skipped Topic" in out


def test_rewrite_wikilinks_handles_case_insensitive_match():
    """LLM frequently lower-cases the second word of a multi-word title.
    The rewriter must still resolve the link to the canonical slug."""
    title = "Hong Kong Work-from-Home Policy"
    content = "See [[hong kong work-from-home policy]]."
    out = _rewrite_glossary_wikilinks(content, [title])
    expected_slug = _slugify(title)
    # The link text keeps the LLM-emitted casing; the slug is canonical.
    assert f"](/wiki/{expected_slug})" in out


def test_build_compiled_topic_slug_index_drops_empty():
    index = _build_compiled_topic_slug_index(["Real Topic", "", "  ", None])  # type: ignore[list-item]
    assert "real topic" in index
    assert index["real topic"] == _slugify("Real Topic")
    # Empty/blank entries don't pollute the index.
    assert "" not in index


def test_render_terms_table_emits_markdown_link_for_compiled_topics():
    """When the Glossary's deterministic-fallback path renders the Terms
    table, Related-Topics cells must surface compiled topics as markdown
    links (``[Title](/wiki/<slug>)``) AND must strip skipped topics to
    plain text. Same slug as the topic compiler — same call to
    ``_slugify`` — so the link actually resolves."""
    compiled_title = "Team Discussion on Work From Home"
    skipped_title = "Breakfast Inequality"
    rows = [
        {
            "term": "WFH",
            "definition": "Work from home",
            "first_mentioned_by": "Alice",
            "related_topics": [compiled_title, skipped_title],
        }
    ]
    table = _render_glossary_terms_table(rows, compiled_topic_titles=[compiled_title])
    slug = _slugify(compiled_title)
    assert f"[{compiled_title}](/wiki/{slug})" in table
    # Skipped title appears as plain text — never as a bracketed broken link.
    assert "[[Breakfast Inequality]]" not in table
    assert skipped_title in table


# ── Part 3: Glossary references filtered to compiled topics ─────────────


def test_collect_filters_related_topics_to_compiled_set():
    """When ``compiled_topic_titles`` is supplied, related_topics entries
    on each glossary row that name a skipped topic must be dropped."""
    glossary = [
        {
            "term": "WFH",
            "definition": "Work from home",
            "first_mentioned_by": "Alice",
            "related_topics": ["Compiled One", "Skipped One"],
        }
    ]
    rows = _collect_glossary_entries(glossary, [], compiled_topic_titles=["Compiled One"])
    wfh = next(r for r in rows if r["term"] == "WFH")
    assert wfh["related_topics"] == ["Compiled One"]


def test_collect_skips_cluster_enrichment_for_non_compiled_clusters():
    """Cluster ``key_entities`` should ONLY contribute to ``related_topics``
    when the cluster actually has a compiled page — otherwise the Glossary
    would surface a Related-Topic name that doesn't exist as a wiki page."""
    skipped_cluster = SimpleNamespace(
        title="Skipped Topic",
        key_entities=[{"name": "Beever", "description": "ignored"}],
        key_facts=[],
    )
    compiled_cluster = SimpleNamespace(
        title="Compiled Topic",
        key_entities=[{"name": "Beever", "description": "kept"}],
        key_facts=[],
    )
    rows = _collect_glossary_entries(
        [],
        [skipped_cluster, compiled_cluster],
        compiled_topic_titles=["Compiled Topic"],
    )
    beever = next(r for r in rows if r["term"] == "Beever")
    # Only the compiled cluster's title appears under related_topics.
    assert beever["related_topics"] == ["Compiled Topic"]


def test_collect_legacy_call_without_compiled_set_keeps_all_titles():
    """Back-compat — callers that don't pass ``compiled_topic_titles`` get
    the legacy behaviour where every cluster title counts."""
    cluster = SimpleNamespace(
        title="Anything",
        key_entities=[{"name": "Beever", "description": "kept"}],
        key_facts=[],
    )
    rows = _collect_glossary_entries([], [cluster])
    beever = next(r for r in rows if r["term"] == "Beever")
    assert beever["related_topics"] == ["Anything"]
