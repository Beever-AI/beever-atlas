"""Hypothesis property tests for escape_gfm_cell (Phase 4)."""

from __future__ import annotations

import markdown_it
from hypothesis import given, settings
from hypothesis import strategies as st

from beever_atlas.wiki.render import escape_gfm_cell


_MD = markdown_it.MarkdownIt().enable("table")


@given(st.text())
@settings(deadline=None, max_examples=50, derandomize=True)
def test_escape_gfm_cell_property(raw: str) -> None:
    """For any Unicode input, embedding the escape in a GFM cell yields
    exactly one header cell that parses cleanly."""
    escaped = escape_gfm_cell(raw)
    # No raw control chars that would break the row.
    assert "\r" not in escaped
    assert "\n" not in escaped
    # Embed as a single-column table header.
    doc = f"| {escaped} |\n|---|\n"
    tokens = _MD.parse(doc)
    th_opens = [t for t in tokens if t.type == "th_open"]
    # Exactly one header cell — no accidental column breaks.
    assert len(th_opens) == 1, (
        f"Expected 1 header cell, got {len(th_opens)} for raw={raw!r} escaped={escaped!r}"
    )
