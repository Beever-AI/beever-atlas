"""Tests for deterministic Key Facts table renderer (Phase 4)."""

from __future__ import annotations

from beever_atlas.wiki.render import escape_gfm_cell, render_key_facts_table


def _fact(text: str, source: str = "alice", ftype: str = "claim",
          importance: float = 0.5, quality_score: float = 0.5) -> dict:
    return {
        "memory_text": text,
        "author_name": source,
        "fact_type": ftype,
        "importance": importance,
        "quality_score": quality_score,
    }


def test_render_key_facts_table_basic() -> None:
    facts = [
        _fact("low", importance=0.1),
        _fact("mid", importance=0.5),
        _fact("top", importance=0.9),
        _fact("mid2", importance=0.5, quality_score=0.9),
        _fact("lowest", importance=0.0),
    ]
    out = render_key_facts_table(facts)
    lines = out.splitlines()
    assert lines[0] == "| Fact | Source | Type | Importance |"
    assert lines[1] == "|------|--------|------|------------|"
    # 5 rows
    assert len(lines) == 2 + 5
    # Sorted by importance desc, then quality_score desc: top, mid2, mid, low, lowest
    assert "top" in lines[2]
    assert "mid2" in lines[3]
    assert "mid" in lines[4]
    assert "low" in lines[5]
    assert "lowest" in lines[6]


def test_render_key_facts_table_escapes_pipes() -> None:
    facts = [_fact("a|b")]
    out = render_key_facts_table(facts)
    assert "a\\|b" in out


def test_render_key_facts_table_escapes_newlines() -> None:
    facts = [_fact("line1\nline2")]
    out = render_key_facts_table(facts)
    assert "line1<br>line2" in out


def test_render_key_facts_table_empty() -> None:
    assert render_key_facts_table([]) == ""


def test_render_key_facts_table_caps_at_max_rows() -> None:
    facts = [_fact(f"f{i}", importance=float(i)) for i in range(20)]
    out = render_key_facts_table(facts, max_rows=8)
    # 2 header lines + 8 rows
    assert len(out.splitlines()) == 10


def test_escape_gfm_cell_backslash_pipe_collision() -> None:
    out = escape_gfm_cell("a\\|b")
    # Embed inside a cell and parse — round-trip via markdown-it-py.
    import markdown_it
    md = markdown_it.MarkdownIt().enable("table")
    doc = f"| {out} |\n|---|\n"
    tokens = md.parse(doc)
    # Expect a single table with one header cell.
    th_opens = [t for t in tokens if t.type == "th_open"]
    assert len(th_opens) == 1, f"Expected 1 header cell, got tokens: {tokens}"


def test_escape_gfm_cell_zero_width() -> None:
    assert escape_gfm_cell("a\u200bb") == "ab"
    assert escape_gfm_cell("\ufeff\u200c\u200d") == " "


def test_escape_gfm_cell_empty_returns_space() -> None:
    assert escape_gfm_cell("") == " "
    assert escape_gfm_cell("   ") == " "
