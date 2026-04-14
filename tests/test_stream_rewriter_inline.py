"""Regression tests for WS-M3: per-tag inline flag routing.

A combined bracket like `[src:src_a, src:src_b inline]` must mark ONLY the
second source as inline in the registry; the first must remain non-inline.
The previous code risked coupling the inline flag to the whole bracket.
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


def test_combined_bracket_mixed_inline_flags_routes_per_tag() -> None:
    r, [a, b] = _registry_with("A", "B")
    rw = StreamRewriter(r)
    # Second tag is inline, first is not.
    text = f"see [src:{a}, src:{b} inline] here"
    out = rw.feed(text) + rw.flush()
    assert out == "see [1] [2] here"

    env = r.finalize()
    by_marker = {ref.marker: ref for ref in env.refs}
    assert by_marker[1].inline is False, "first tag must NOT be inline"
    assert by_marker[2].inline is True, "second tag MUST be inline"


def test_combined_bracket_first_inline_second_not() -> None:
    r, [a, b] = _registry_with("A", "B")
    rw = StreamRewriter(r)
    text = f"see [src:{a} inline, src:{b}] here"
    out = rw.feed(text) + rw.flush()
    assert out == "see [1] [2] here"

    env = r.finalize()
    by_marker = {ref.marker: ref for ref in env.refs}
    assert by_marker[1].inline is True
    assert by_marker[2].inline is False


def test_combined_bracket_both_inline() -> None:
    r, [a, b] = _registry_with("A", "B")
    rw = StreamRewriter(r)
    text = f"see [src:{a} inline, src:{b} inline] here"
    rw.feed(text)
    rw.flush()
    env = r.finalize()
    assert all(ref.inline for ref in env.refs)


def test_combined_bracket_neither_inline() -> None:
    r, [a, b] = _registry_with("A", "B")
    rw = StreamRewriter(r)
    text = f"see [src:{a}, src:{b}] here"
    rw.feed(text)
    rw.flush()
    env = r.finalize()
    assert all(ref.inline is False for ref in env.refs)
