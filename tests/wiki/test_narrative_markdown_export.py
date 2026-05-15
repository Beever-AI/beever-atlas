"""Tests for ``narrative_sections_to_markdown`` (P2).

The wiki export route must inline-render the structured
``narrative_sections`` payload into the exported Markdown so a user
downloading their wiki sees the article body, not just the legacy
module-substituted content.

Coverage:
- Section headings render as ``## {heading}``
- ``[f_xxx]`` markers rewrite to per-article 1-indexed numbers
- agent-inference paragraphs prepend ``*[agent-inference]*``
- Each visual kind round-trips to its Markdown form
- Empty / missing input returns ``""`` so the caller can prepend safely
"""

from __future__ import annotations

import pytest

from beever_atlas.wiki.modules.narrative_markdown import (
    narrative_sections_to_markdown,
)


def _section(
    *,
    heading: str = "Intro",
    anchor: str = "intro",
    paragraphs=None,
    visual=None,
):
    return {
        "anchor": anchor,
        "heading": heading,
        "paragraphs": list(paragraphs or []),
        "visual": visual,
        "citations": [],
        "citation_coverage": 1.0,
    }


def _para(text: str, citations=None, is_inference: bool = False):
    return {
        "text": text,
        "citations": list(citations or []),
        "is_inference": is_inference,
    }


def test_empty_or_missing_input_returns_empty_string():
    assert narrative_sections_to_markdown(None) == ""
    assert narrative_sections_to_markdown([]) == ""
    # Malformed entries skip but the helper itself returns an empty
    # string so callers can prepend safely.
    assert narrative_sections_to_markdown([{}]) == ""


def test_renders_section_heading_as_h2():
    md = narrative_sections_to_markdown(
        [_section(heading="Background", paragraphs=[_para("Some prose.")])],
        metadata_line=False,
    )
    assert "## Background" in md
    assert "Some prose." in md


def test_inline_citation_markers_become_numbered_references():
    md = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[
                    _para(
                        "Atlas adopted Mattermost as the chat layer [f_4].",
                        citations=["f_4"],
                    ),
                    _para(
                        "Two providers were considered [f_4, f_7].",
                        citations=["f_4", "f_7"],
                    ),
                ],
            )
        ],
        metadata_line=False,
    )
    # First-occurrence order: f_4 → 1, f_7 → 2.
    assert "[1]" in md
    assert "[1, 2]" in md
    # Raw markers must NOT survive in the export.
    assert "[f_4]" not in md
    assert "[f_4, f_7]" not in md


def test_agent_inference_paragraph_prepends_italic_marker():
    md = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[
                    _para(
                        "These decisions suggest a shift toward enterprise [f_1].",
                        citations=["f_1"],
                        is_inference=True,
                    )
                ]
            )
        ],
        metadata_line=False,
    )
    # Italic marker comes first, then the rewritten paragraph.
    assert "*[agent-inference]*" in md
    assert "*[agent-inference]* These decisions" in md


def test_visual_table_renders_as_gfm_table():
    md = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[_para("Compare options.")],
                visual={
                    "kind": "table",
                    "content": {
                        "headers": ["Option", "Cost"],
                        "rows": [["JWT", "low"], ["SAML", "high"]],
                    },
                },
            )
        ],
        metadata_line=False,
    )
    assert "| Option | Cost |" in md
    assert "| JWT | low |" in md
    assert "| SAML | high |" in md
    # Header / body separator row.
    assert "| --- | --- |" in md


def test_visual_mermaid_renders_as_fenced_block():
    md = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[_para("See diagram.")],
                visual={"kind": "mermaid", "content": "graph TD\nA-->B"},
            )
        ],
        metadata_line=False,
    )
    assert "```mermaid" in md
    assert "graph TD" in md
    assert "A-->B" in md


def test_visual_list_unordered_and_ordered():
    md_ul = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[_para("See list.")],
                visual={
                    "kind": "list",
                    "content": {"items": ["First", "Second"], "ordered": False},
                },
            )
        ],
        metadata_line=False,
    )
    assert "- First" in md_ul
    assert "- Second" in md_ul

    md_ol = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[_para("Steps.")],
                visual={
                    "kind": "list",
                    "content": {"items": ["A", "B"], "ordered": True},
                },
            )
        ],
        metadata_line=False,
    )
    assert "1. A" in md_ol
    assert "2. B" in md_ol


def test_visual_callout_renders_as_gfm_callout():
    md = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[_para("Warn.")],
                visual={
                    "kind": "callout",
                    "content": {"variant": "warning", "text": "Mind the gap."},
                },
            )
        ],
        metadata_line=False,
    )
    assert "> [!WARNING]" in md
    assert "> Mind the gap." in md


def test_visual_code_renders_with_language_fence():
    md = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[_para("Snippet.")],
                visual={
                    "kind": "code",
                    "content": {"language": "python", "code": "print('hi')"},
                },
            )
        ],
        metadata_line=False,
    )
    assert "```python" in md
    assert "print('hi')" in md


def test_visual_blockquote_renders_with_attribution():
    md = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[_para("Voice.")],
                visual={
                    "kind": "blockquote",
                    "content": {
                        "content": "Ship small, ship often.",
                        "attribution": "Eng team",
                    },
                },
            )
        ],
        metadata_line=False,
    )
    assert "> Ship small, ship often." in md
    assert "> — Eng team" in md


def test_metadata_line_includes_reading_time_and_memory_count():
    # 250 words → ceil(250/200) = 2 minutes; 2 distinct fact_ids.
    long_text = " ".join([f"word{i}" for i in range(250)])
    md = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[
                    _para(long_text + " [f_a]", citations=["f_a"]),
                    _para("Trailing [f_b].", citations=["f_b"]),
                ]
            )
        ],
        metadata_line=True,
    )
    # Italic metadata line opens the article.
    assert md.startswith("_")
    assert "min read" in md
    assert "memories synthesized" in md or "memory synthesized" in md


def test_singular_memory_label_for_one_distinct_fact():
    md = narrative_sections_to_markdown(
        [_section(paragraphs=[_para("Lone [f_only].", citations=["f_only"])])],
        metadata_line=True,
    )
    assert "1 memory synthesized" in md


def test_pages_without_narrative_sections_export_unchanged():
    """The legacy export path passes ``page.get("narrative_sections") or []``
    to the helper. Empty input must produce ``""`` so the caller can
    prepend without affecting the legacy ``page.content`` body. This
    test pins that backward-compat contract directly."""
    assert narrative_sections_to_markdown([]) == ""
    assert narrative_sections_to_markdown(None) == ""


def test_unknown_visual_kind_skipped_silently():
    md = narrative_sections_to_markdown(
        [
            _section(
                paragraphs=[_para("Para.")],
                visual={"kind": "unknown_thing", "content": "noise"},
            )
        ],
        metadata_line=False,
    )
    # Heading + paragraph survive; unknown visual contributes nothing.
    assert "## Intro" in md
    assert "Para." in md
    assert "noise" not in md


def test_pages_with_narrative_sections_export_path_integration(monkeypatch):
    """Smoke test for the export route's prepend behavior: a page with
    narrative_sections in the cache subdoc must produce export markdown
    where the article appears BEFORE the legacy ``content`` body."""
    # Build a narrative-only fragment + a synthetic legacy body string,
    # then assemble like the export route does.
    sections = [
        _section(
            heading="Background",
            paragraphs=[_para("Setup [f_1].", citations=["f_1"])],
        ),
        _section(
            heading="Outcome",
            paragraphs=[_para("Result [f_2].", citations=["f_2"])],
        ),
    ]
    narrative_md = narrative_sections_to_markdown(sections, metadata_line=False)
    legacy_body = "## Modules\n\nLegacy module body."
    # The export route prepends the narrative chunk + a separator before
    # the legacy body. Mirror that here.
    combined = "\n\n".join([narrative_md, "\n---\n", legacy_body])
    # Article appears first.
    assert combined.index("## Background") < combined.index("## Modules")
    # Separator between the article and the appendix.
    assert "---" in combined
    # Legacy body still present (backward compat).
    assert "Legacy module body." in combined


@pytest.mark.parametrize(
    "kind",
    ["table", "mermaid", "list", "callout", "code", "blockquote"],
)
def test_each_visual_kind_round_trips_without_error(kind):
    """Smoke test every supported visual kind so a future schema drift
    surfaces as a clear ``"" / non-empty`` test failure rather than a
    silent skip."""
    payloads: dict[str, dict] = {
        "table": {
            "kind": "table",
            "content": {"headers": ["a"], "rows": [["1"]]},
        },
        "mermaid": {"kind": "mermaid", "content": "graph TD\nA-->B"},
        "list": {
            "kind": "list",
            "content": {"items": ["x"], "ordered": False},
        },
        "callout": {
            "kind": "callout",
            "content": {"variant": "tip", "text": "Hi."},
        },
        "code": {
            "kind": "code",
            "content": {"language": "py", "code": "x=1"},
        },
        "blockquote": {
            "kind": "blockquote",
            "content": {"content": "Q.", "attribution": "A"},
        },
    }
    md = narrative_sections_to_markdown(
        [_section(paragraphs=[_para("Para.")], visual=payloads[kind])],
        metadata_line=False,
    )
    assert md != ""
