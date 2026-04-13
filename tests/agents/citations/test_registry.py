"""Unit tests for the citation SourceRegistry.

Covers id derivation, dedup, score merging, attachment merging, excerpt
truncation, marker assignment, inline downgrade, and drop-unused finalize.
"""

from __future__ import annotations

import pytest

from beever_atlas.agents.citations.registry import (
    SourceRegistry,
    _derive_id,
    _truncate_excerpt,
    bind,
    current_registry,
    reset,
)
from beever_atlas.agents.citations.types import MediaAttachment


# ---- id derivation ----------------------------------------------------


def test_derive_id_stable():
    a = _derive_id("channel_message", "slack:C1:123.456:fact-0")
    b = _derive_id("channel_message", "slack:C1:123.456:fact-0")
    assert a == b
    assert a.startswith("src_") and len(a) == 14  # "src_" + 10 hex


def test_derive_id_kind_sensitive():
    a = _derive_id("channel_message", "x")
    b = _derive_id("web_result", "x")
    assert a != b


# ---- excerpt truncation ----------------------------------------------


def test_truncate_excerpt_preserves_short():
    assert _truncate_excerpt("hello world") == "hello world"


def test_truncate_excerpt_respects_cap():
    long_text = "word " * 500
    out = _truncate_excerpt(long_text)
    assert len(out) <= 401  # 400 + trailing ellipsis char
    assert out.endswith("…")


def test_truncate_excerpt_empty():
    assert _truncate_excerpt("") == ""
    assert _truncate_excerpt(None) == ""


# ---- registration ------------------------------------------------------


def _base_kwargs(**overrides):
    d = dict(
        kind="channel_message",
        native_identity="slack:C1:1.1:f0",
        native={"platform": "slack", "channel_id": "C1"},
        title="t",
        excerpt="hello world",
        retrieved_by={"tool": "search_channel_facts", "query": "x", "score": 0.5},
    )
    d.update(overrides)
    return d


def test_register_returns_id():
    r = SourceRegistry()
    sid = r.register(**_base_kwargs())
    assert sid is not None
    assert sid.startswith("src_")
    assert r.registered_count == 1


def test_register_empty_excerpt_skipped():
    r = SourceRegistry()
    sid = r.register(**_base_kwargs(excerpt=""))
    assert sid is None
    assert r.registered_count == 0


def test_register_dedup_same_identity():
    r = SourceRegistry()
    a = r.register(**_base_kwargs())
    b = r.register(**_base_kwargs())
    assert a == b
    assert r.registered_count == 1


def test_register_dedup_merges_attachments():
    r = SourceRegistry()
    r.register(
        **_base_kwargs(
            attachments=[MediaAttachment(kind="image", url="https://a/1.png")]
        )
    )
    r.register(
        **_base_kwargs(
            attachments=[
                MediaAttachment(kind="image", url="https://a/1.png"),  # dup
                MediaAttachment(kind="image", url="https://a/2.png"),  # new
            ]
        )
    )
    assert r.registered_count == 1
    source = list(r._sources.values())[0]
    assert len(source.attachments) == 2
    assert [a.url for a in source.attachments] == [
        "https://a/1.png",
        "https://a/2.png",
    ]


def test_register_score_accumulates_max_wise():
    r = SourceRegistry()
    r.register(**_base_kwargs(retrieved_by={"tool": "t", "query": "q", "score": 0.3}))
    r.register(**_base_kwargs(retrieved_by={"tool": "t", "query": "q", "score": 0.8}))
    r.register(**_base_kwargs(retrieved_by={"tool": "t", "query": "q", "score": 0.5}))
    source = list(r._sources.values())[0]
    assert source.retrieved_by["score"] == 0.8


# ---- marker assignment -------------------------------------------------


def test_mark_referenced_unknown_returns_false():
    r = SourceRegistry()
    assert r.mark_referenced("src_notfound", 1) is False


def test_mark_referenced_inline_downgraded_without_attachments():
    r = SourceRegistry()
    sid = r.register(**_base_kwargs())
    r.mark_referenced(sid, 1, inline=True)
    env = r.finalize()
    assert env.refs[0].inline is False  # downgraded — no attachments


def test_mark_referenced_inline_preserved_with_attachments():
    r = SourceRegistry()
    sid = r.register(
        **_base_kwargs(
            attachments=[MediaAttachment(kind="image", url="https://a/1.png")]
        )
    )
    r.mark_referenced(sid, 1, inline=True)
    env = r.finalize()
    assert env.refs[0].inline is True


def test_mark_referenced_inline_sticks():
    r = SourceRegistry()
    sid = r.register(
        **_base_kwargs(
            attachments=[MediaAttachment(kind="image", url="https://a/1.png")]
        )
    )
    r.mark_referenced(sid, 1, inline=False)
    r.mark_referenced(sid, 1, inline=True)
    env = r.finalize()
    assert env.refs[0].inline is True


# ---- finalize ----------------------------------------------------------


def test_finalize_drops_unreferenced():
    r = SourceRegistry()
    sid_a = r.register(**_base_kwargs(native_identity="A"))
    r.register(**_base_kwargs(native_identity="B"))  # never referenced
    r.mark_referenced(sid_a, 1)
    env = r.finalize()
    assert len(env.sources) == 1
    assert env.sources[0].id == sid_a
    assert [ref.source_id for ref in env.refs] == [sid_a]


def test_finalize_empty_when_no_markers():
    r = SourceRegistry()
    r.register(**_base_kwargs())
    env = r.finalize()
    assert env.items == []
    assert env.sources == []
    assert env.refs == []


def test_finalize_preserves_first_appearance_order():
    r = SourceRegistry()
    a = r.register(**_base_kwargs(native_identity="A"))
    b = r.register(**_base_kwargs(native_identity="B"))
    c = r.register(**_base_kwargs(native_identity="C"))
    r.mark_referenced(b, 1)
    r.mark_referenced(a, 2)
    r.mark_referenced(c, 3)
    env = r.finalize()
    assert [s.id for s in env.sources] == [b, a, c]


def test_finalize_builds_legacy_items():
    r = SourceRegistry()
    sid = r.register(
        **_base_kwargs(
            native={
                "platform": "slack",
                "channel_id": "C1",
                "channel_name": "general",
                "author": "alice",
                "timestamp": "2026-01-01",
            },
        )
    )
    r.mark_referenced(sid, 1)
    env = r.finalize()
    assert len(env.items) == 1
    item = env.items[0]
    assert item["author"] == "alice"
    assert item["channel"] == "general"
    assert item["timestamp"] == "2026-01-01"
    assert item["number"] == "1"
    assert item["text"] == "hello world"


# ---- contextvar lifecycle ---------------------------------------------


def test_contextvar_isolation():
    assert current_registry() is None
    r1, tok1 = bind(session_id="s1")
    try:
        assert current_registry() is r1
    finally:
        reset(tok1)
    assert current_registry() is None


def test_contextvar_reset_after_exception():
    r, tok = bind()
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            reset(tok)
    except Exception:
        pytest.fail("reset should succeed after exception")
    assert current_registry() is None
