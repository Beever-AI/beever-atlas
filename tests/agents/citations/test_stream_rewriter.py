"""Unit tests for the stream rewriter.

Covers first-appearance numbering, inline modifier parsing, chunk-boundary
safety, unknown-tag stripping, and registry marker recording.
"""

from __future__ import annotations

from beever_atlas.agents.citations.registry import SourceRegistry
from beever_atlas.agents.citations.types import MediaAttachment
from beever_atlas.agents.query.stream_rewriter import StreamRewriter


def _registry_with(*identities):
    r = SourceRegistry()
    ids = []
    for native in identities:
        sid = r.register(
            kind="channel_message",
            native_identity=native,
            native={"platform": "slack", "channel_id": "C1"},
            title="t",
            excerpt="hello",
            retrieved_by={},
            attachments=[MediaAttachment(kind="image", url=f"https://a/{native}.png")],
        )
        ids.append(sid)
    return r, ids


def _collect(rw: StreamRewriter, chunks):
    out = []
    for c in chunks:
        out.append(rw.feed(c))
    out.append(rw.flush())
    return "".join(out)


def test_first_appearance_order():
    r, [a, b] = _registry_with("A", "B")
    rw = StreamRewriter(r)
    text = f"go [src:{b}] then [src:{a}] again [src:{b}]"
    out = _collect(rw, [text])
    assert out == "go [1] then [2] again [1]"


def test_plain_tag_marks_inline_false():
    r, [a] = _registry_with("A")
    rw = StreamRewriter(r)
    _collect(rw, [f"x [src:{a}] y"])
    env = r.finalize()
    assert env.refs[0].inline is False


def test_inline_tag_marks_inline_true():
    r, [a] = _registry_with("A")
    rw = StreamRewriter(r)
    _collect(rw, [f"see it [src:{a} inline] here"])
    env = r.finalize()
    assert env.refs[0].inline is True


def test_inline_word_not_leaked_to_stream():
    r, [a] = _registry_with("A")
    rw = StreamRewriter(r)
    out = _collect(rw, [f"x [src:{a} inline] y"])
    assert "inline" not in out
    assert out == "x [1] y"


def test_chunk_boundary_mid_tag():
    r, [a] = _registry_with("A")
    rw = StreamRewriter(r)
    mid = f"[src:{a}]"
    # Split at various positions.
    chunks = ["pre ", mid[:4], mid[4:10], mid[10:], " post"]
    out = _collect(rw, chunks)
    assert out == "pre [1] post"


def test_chunk_boundary_inside_inline_modifier():
    r, [a] = _registry_with("A")
    rw = StreamRewriter(r)
    full = f"before [src:{a} inline] after"
    # Split right inside the 'inline' word.
    mid = full.index("inline") + 3
    out = _collect(rw, [full[:mid], full[mid:]])
    assert out == "before [1] after"
    env = r.finalize()
    assert env.refs[0].inline is True


def test_unknown_tag_stripped():
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    # Well-formed tag (matches regex) but the source_id isn't registered.
    out = _collect(rw, ["before [src:src_deadbeef00] after"])
    assert out == "before  after"
    assert rw.unknown_tag_count == 1


def test_malformed_tag_stripped_at_stream_time():
    """Malformed `[src:tool_name_response]`-style literals must be scrubbed
    from `response_delta` output, not just at flush. Without this, the
    client sees the raw literal render in the UI before the stream ends."""
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    # Invalid hex — doesn't match the strict [src:src_<10hex>] pattern,
    # so it's treated as a leftover literal and stripped during streaming.
    out = _collect(rw, ["x [src:notvalid] y"])
    assert out == "x  y"
    assert "[src:" not in out


def test_bogus_tool_name_literal_stripped_midstream():
    """Regression for the UX-visible `[src:get_wiki_page_response]` leak:
    the stripper runs on every drain output, not only at flush, so the
    literal never reaches `response_delta` events."""
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    # Feed a single chunk that already contains the full bogus literal —
    # this is the path that previously leaked because `_find_open_tag`
    # saw the closing `]` and didn't hold the buffer back.
    emitted = rw.feed(
        "There is no wiki content [src:get_wiki_page_response]. Sync it?"
    )
    assert "[src:" not in emitted
    assert emitted == "There is no wiki content . Sync it?"
    # Flush is a no-op now because the literal was already removed.
    assert rw.flush() == ""


def test_non_tag_brackets_pass_through():
    r, [a] = _registry_with("A")
    rw = StreamRewriter(r)
    text = f"see [1] and [src:{a}] and [foo]"
    out = _collect(rw, [text])
    assert out == "see [1] and [1] and [foo]"


def test_many_chunks_single_char():
    r, [a] = _registry_with("A")
    rw = StreamRewriter(r)
    text = f"x [src:{a}] y"
    out = _collect(rw, list(text))
    assert out == "x [1] y"


def test_flush_emits_remaining_open_tag_literally():
    # Edge: stream ends with an incomplete tag that never closes.
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    out = rw.feed("tail [src:src_abc")
    tail = rw.flush()
    # The literal should appear in the output (literal passthrough).
    assert "tail " in out or "tail " in tail
    combined = out + tail
    assert "[src:src_abc" in combined


def test_repeated_inline_upgrades_existing_ref():
    r, [a] = _registry_with("A")
    rw = StreamRewriter(r)
    _collect(rw, [f"first [src:{a}] then [src:{a} inline]"])
    env = r.finalize()
    assert env.refs[0].inline is True


def test_registry_order_matches_rewriter_assignments():
    r, [a, b, c] = _registry_with("A", "B", "C")
    rw = StreamRewriter(r)
    _collect(rw, [f"[src:{c}] [src:{b}] [src:{a}]"])
    env = r.finalize()
    assert [ref.marker for ref in env.refs] == [1, 2, 3]
    assert [s.id for s in env.sources] == [c, b, a]


# ---- combined-tag forms (regression for critical live bug) -----------


def test_combined_tags_with_comma():
    """LLM emits [src:a, src:b, src:c] — must become [1] [2] [3]."""
    r, [a, b, c] = _registry_with("A", "B", "C")
    rw = StreamRewriter(r)
    out = _collect(rw, [f"settled [src:{a}, src:{b}, src:{c}] for real"])
    assert out == "settled [1] [2] [3] for real"
    env = r.finalize()
    assert {ref.source_id for ref in env.refs} == {a, b, c}


def test_combined_tags_mixed_with_single():
    r, [a, b, c] = _registry_with("A", "B", "C")
    rw = StreamRewriter(r)
    out = _collect(
        rw,
        [f"first [src:{a}] then [src:{b}, src:{c}] done"],
    )
    assert out == "first [1] then [2] [3] done"


def test_combined_with_inline_modifier():
    r, [a, b] = _registry_with("A", "B")
    r.register(
        kind="channel_message",
        native_identity="A",
        native={"platform": "slack", "channel_id": "C1"},
        title="t",
        excerpt="hello",
        retrieved_by={},
        attachments=[MediaAttachment(kind="image", url="https://a/x.png")],
    )
    rw = StreamRewriter(r)
    out = _collect(rw, [f"[src:{a} inline, src:{b}]"])
    # Inline form keeps producing [N] in text; the inline flag lives on the ref.
    assert out == "[1] [2]"
    env = r.finalize()
    a_ref = next(ref for ref in env.refs if ref.source_id == a)
    b_ref = next(ref for ref in env.refs if ref.source_id == b)
    assert a_ref.inline is True
    assert b_ref.inline is False


def test_combined_all_unknown_strips_whole_bracket():
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    out = _collect(
        rw,
        ["pre [src:src_deadbeef01, src:src_deadbeef02] post"],
    )
    assert out == "pre  post"


def test_combined_partial_unknown_keeps_known():
    r, [a] = _registry_with("A")
    rw = StreamRewriter(r)
    out = _collect(
        rw,
        [f"pre [src:{a}, src:src_deadbeef03] post"],
    )
    # Unknown is silently stripped from the bracket; known becomes [1].
    assert out == "pre [1] post"


def test_flush_strips_leftover_src_literal():
    """Malformed tag that survives the main pass is stripped at flush."""
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    # Feed something the main loop passes through literally (regex
    # doesn't match — no src_<hex>), then flush must strip it.
    # We simulate by forcing a literal into the buffer at flush time.
    rw._buffer = "tail [src:oops-not-hex] end"  # noqa: SLF001
    out = rw.flush()
    assert "[src:" not in out
    assert rw.leftover_stripped_count == 1


def test_get_stats_tracks_unknown_tags():
    """Counter increments for unknown src: tags."""
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    _collect(rw, ["before [src:src_deadbeef00] and [src:src_deadbeef11] after"])
    stats = rw.get_stats()
    assert stats["unknown_tags"] == 2
    assert stats["orphan_markers"] == 0


def test_cap_flush_strips_partial_src_opener():
    """H8: when buffer cap is tripped, partial [src:... is stripped."""
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    from beever_atlas.agents.query.stream_rewriter import _MAX_BUFFER

    # Feed a partial opener then non-matching bytes that overflow the cap
    # without ever producing `]`. Simulates LLM emitting a malformed
    # non-closing tag amid a firehose.
    first = rw.feed("abc [src:src_12")
    # Drive the drain via a subsequent feed that pushes us past the cap.
    second = rw.feed("z" * (_MAX_BUFFER + 5))
    combined = first + second
    # Partial opener must not leak to the client in either chunk.
    assert "[src:" not in combined
    # The safe prefix `abc ` should have been emitted across the stream.
    assert combined.startswith("abc ")


def test_open_tag_detects_combined_partial_at_boundary():
    """`[src:a, src:` at chunk end must buffer until closing `]` arrives."""
    r, [a, b] = _registry_with("A", "B")
    rw = StreamRewriter(r)
    full = f"pre [src:{a}, src:{b}] post"
    # Split in the middle of the comma-separated content.
    mid = full.index(", ")
    out = _collect(rw, [full[:mid], full[mid:]])
    assert out == "pre [1] [2] post"


# ---- leftover tag stripping: src and External shapes ---------------------


def test_flush_strips_leftover_src_tag():
    """_strip_leftovers removes a malformed [src:...] literal at flush time.

    Tags with valid 10-hex IDs are caught earlier by the main pass (as
    unknown-tag stripped). _strip_leftovers handles the residual cases:
    malformed hex, extra spaces, etc. — whatever slipped through.
    """
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    # Non-hex content — regex won't match the main pass, so it reaches flush.
    rw._buffer = "before [src:malformed-not-hex] after"  # noqa: SLF001
    out = rw.flush()
    assert "[src:" not in out
    assert "before" in out
    assert "after" in out
    assert rw.leftover_stripped_count == 1


def test_flush_strips_leftover_external_tag():
    """_strip_leftovers removes [External: some_url] at flush time."""
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    rw._buffer = "see [External: https://example.com] for more"  # noqa: SLF001
    out = rw.flush()
    assert "[External:" not in out
    assert "see" in out
    assert "for more" in out
    assert rw.leftover_stripped_count == 1


def test_flush_strips_both_leftover_shapes():
    """_strip_leftovers handles both [src:...] and [External: ...] in one pass."""
    r, _ = _registry_with("A")
    rw = StreamRewriter(r)
    rw._buffer = "a [src:bad-hex] b [External: https://x.com/page] c"  # noqa: SLF001
    out = rw.flush()
    assert "[src:" not in out
    assert "[External:" not in out
    assert "a" in out and "b" in out and "c" in out
    assert rw.leftover_stripped_count == 2
