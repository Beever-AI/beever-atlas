"""Tests for `LiteralSrcStripper` — the registry-independent `[src:...]` scrubber.

These guard the UI leak where Gemini hallucinates citation markers using
tool names (e.g. `[src:get_topic_overview_response]`) while the citation
registry flag is OFF. The stripper must run unconditionally in that case.
"""

from __future__ import annotations

from beever_atlas.agents.query.stream_rewriter import LiteralSrcStripper


def test_strips_tool_name_src_literal() -> None:
    rw = LiteralSrcStripper()
    out = rw.feed("Hello [src:get_topic_overview_response] world")
    tail = rw.flush()
    assert out + tail == "Hello  world"


def test_strips_multiple_literals() -> None:
    rw = LiteralSrcStripper()
    text = (
        "Start [src:foo_response] middle [src:bar_response] "
        "and [src:baz_response] end"
    )
    out = rw.feed(text) + rw.flush()
    assert "[src:" not in out
    assert "Start " in out
    assert " end" in out
    # All three literals removed.
    assert out == "Start  middle  and  end"


def test_preserves_valid_src_like_text() -> None:
    rw = LiteralSrcStripper()
    out1 = rw.feed("Check [not a src]")
    out2 = rw.feed(" and [1] and [2]")
    tail = rw.flush()
    full = out1 + out2 + tail
    assert full == "Check [not a src] and [1] and [2]"


def test_buffers_truncated_opener_across_chunks() -> None:
    rw = LiteralSrcStripper()
    part1 = rw.feed("Before [src:")
    # The truncated opener must be held back so the client never sees it.
    assert part1 == "Before "
    part2 = rw.feed("tool_name_response] after")
    # Chunk 2 emits everything minus the completed `[src:...]` literal.
    assert part2 == " after"
    tail = rw.flush()
    assert tail == ""


def test_flush_drops_dangling_truncated_opener() -> None:
    rw = LiteralSrcStripper()
    emitted = rw.feed("Text [src:unclose")
    # Opener held back mid-stream.
    assert emitted == "Text "
    tail = rw.flush()
    # Dangling opener must be dropped on flush — never ship `[src:unclose`.
    assert tail == ""
    assert "[src:" not in (emitted + tail)


def test_idempotent_on_clean_text() -> None:
    rw = LiteralSrcStripper()
    out1 = rw.feed("Plain text with ")
    out2 = rw.feed("no citation markers at all.")
    tail = rw.flush()
    assert out1 + out2 == "Plain text with no citation markers at all."
    assert tail == ""
