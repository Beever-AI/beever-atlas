"""Tests for the v2 ``key_facts`` module — frontend renderer.

Covers:
  (a) renderer_kind transition (catalog says ``frontend``)
  (b) data shape correctness (items / groups / per-item fields)
  (c) sort order (importance DESC then date DESC)
  (d) grouping vocabulary (decision/observation/open_question/
      action_item/opinion)
  (e) fact_type humanization happens via the frontend; backend
      normalizes raw values
  (f) URL hyperlinking happens client-side; backend preserves the
      raw text containing http(s) URLs

These tests exercise the pure ``build_key_facts_data`` builder so
they run without LLM, network, or DB.
"""

from __future__ import annotations

from beever_atlas.wiki.modules import MODULE_CATALOG, key_facts


def _make_fact(
    *,
    fact_id: str = "f-default",
    memory_text: str = "Default fact body.",
    fact_type: str = "observation",
    importance: object = "medium",
    author_name: str = "",
    ts: str = "",
    permalink: str = "",
) -> dict:
    """Helper following the existing ``_make_fact`` pattern in tests."""
    return {
        "fact_id": fact_id,
        "memory_text": memory_text,
        "fact_type": fact_type,
        "importance": importance,
        "author_name": author_name,
        "message_ts": ts,
        "permalink": permalink,
    }


# ---------------------------------------------------------------------------
# (a) renderer_kind transition
# ---------------------------------------------------------------------------


def test_key_facts_catalog_renderer_kind_is_frontend() -> None:
    """The v2 transition flips the catalog entry to ``frontend`` so
    the React component owns rendering. Re-asserting here in addition
    to the catalog test guards against accidental flips back to
    python while v1 backwards-compat is being maintained."""
    spec = MODULE_CATALOG["key_facts"]
    assert spec.renderer_kind == "frontend"


# ---------------------------------------------------------------------------
# (b) data shape
# ---------------------------------------------------------------------------


def test_build_key_facts_data_emits_expected_top_level_shape() -> None:
    out = key_facts.build_key_facts_data([_make_fact()])
    assert out["label"] == "Key Facts"
    assert out["renderer_kind"] == "frontend"
    assert isinstance(out["items"], list)
    assert isinstance(out["groups"], list)
    # Default group ordering matches the spec.
    assert out["groups"] == [
        "decision",
        "observation",
        "open_question",
        "action_item",
        "opinion",
    ]


def test_build_key_facts_data_per_item_fields() -> None:
    facts = [
        _make_fact(
            fact_id="f1",
            memory_text="JWT replaces SAML across all auth services.",
            fact_type="decision",
            importance="critical",
            author_name="Alice",
            ts="2026-04-15T12:00:00Z",
            permalink="https://example.com/msg/1",
        )
    ]
    out = key_facts.build_key_facts_data(facts)
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["fact_id"] == "f1"
    assert "JWT replaces SAML" in item["title"]
    assert item["body"].startswith("JWT replaces SAML")
    assert item["fact_type"] == "decision"
    assert item["importance"] == "critical"
    assert item["author"] == {"name": "Alice", "id": ""}
    assert item["ts"] == "2026-04-15T12:00:00Z"
    assert item["source"] == {"url": "https://example.com/msg/1", "platform": ""}
    assert item["citations"] == []


def test_build_key_facts_data_empty_input() -> None:
    out = key_facts.build_key_facts_data([])
    assert out["items"] == []
    out = key_facts.build_key_facts_data(None)  # type: ignore[arg-type]
    assert out["items"] == []


def test_build_key_facts_data_uses_first_sentence_for_title() -> None:
    facts = [
        _make_fact(
            memory_text=(
                "JWT replaces SAML for service auth. "
                "Token TTL was set to 24 hours after security review."
            )
        )
    ]
    item = key_facts.build_key_facts_data(facts)["items"][0]
    assert item["title"] == "JWT replaces SAML for service auth."
    # Body keeps the full text.
    assert "Token TTL" in item["body"]


def test_build_key_facts_data_truncates_long_titles_at_word_boundary() -> None:
    long_text = (
        "This sentence has no terminator and goes on for a very very very "
        "very very very very very very very very long length to exercise "
        "the truncation behavior of the title builder beyond limits"
    )
    item = key_facts.build_key_facts_data([_make_fact(memory_text=long_text)])["items"][0]
    # Title is bounded; either ends at a word boundary with "…" or at
    # the cap with "…".
    assert len(item["title"]) <= 141  # 140 + ellipsis


# ---------------------------------------------------------------------------
# (c) sort order
# ---------------------------------------------------------------------------


def test_build_key_facts_data_sorts_by_importance_then_date_desc() -> None:
    facts = [
        _make_fact(fact_id="low_old", importance="low", ts="2026-01-01"),
        _make_fact(fact_id="crit_old", importance="critical", ts="2026-01-01"),
        _make_fact(fact_id="crit_new", importance="critical", ts="2026-04-01"),
        _make_fact(fact_id="med_new", importance="medium", ts="2026-04-15"),
        _make_fact(fact_id="high_mid", importance="high", ts="2026-03-01"),
    ]
    out = key_facts.build_key_facts_data(facts)
    ids = [it["fact_id"] for it in out["items"]]
    # critical first (newest first within tier), then high, then
    # medium, then low.
    assert ids == ["crit_new", "crit_old", "high_mid", "med_new", "low_old"]


def test_build_key_facts_data_normalizes_numeric_importance() -> None:
    facts = [
        _make_fact(fact_id="num9", importance=9),  # → critical
        _make_fact(fact_id="num7", importance=7),  # → high
        _make_fact(fact_id="num5", importance=5),  # → medium
        _make_fact(fact_id="num1", importance=1),  # → low
    ]
    out = key_facts.build_key_facts_data(facts)
    sev = {it["fact_id"]: it["importance"] for it in out["items"]}
    assert sev == {
        "num9": "critical",
        "num7": "high",
        "num5": "medium",
        "num1": "low",
    }


# ---------------------------------------------------------------------------
# (d) grouping vocabulary
# ---------------------------------------------------------------------------


def test_build_key_facts_data_groups_default_order_present() -> None:
    """The ``groups`` field declares the canonical group order. The
    frontend uses this to render group sections in a stable sequence."""
    out = key_facts.build_key_facts_data([_make_fact()])
    assert "decision" in out["groups"]
    assert "observation" in out["groups"]
    assert "open_question" in out["groups"]
    assert "action_item" in out["groups"]
    assert "opinion" in out["groups"]


def test_build_key_facts_data_normalizes_fact_type_strings() -> None:
    """Different fact_type representations from upstream collapse to
    a single normalized form so the frontend's grouping is stable."""
    facts = [
        _make_fact(fact_id="a", fact_type="Open Question"),
        _make_fact(fact_id="b", fact_type="open-question"),
        _make_fact(fact_id="c", fact_type="OPEN_QUESTION"),
        _make_fact(fact_id="d", fact_type=""),
    ]
    out = key_facts.build_key_facts_data(facts)
    types = {it["fact_id"]: it["fact_type"] for it in out["items"]}
    assert types["a"] == "open_question"
    assert types["b"] == "open_question"
    assert types["c"] == "open_question"
    assert types["d"] == "observation"  # empty defaults to observation


# ---------------------------------------------------------------------------
# (e) Type humanization — backend normalizes; frontend humanizes. This
#     test asserts the backend produces the canonical lowercase keys
#     the frontend's humanizer expects.
# ---------------------------------------------------------------------------


def test_build_key_facts_data_canonical_type_keys_for_frontend() -> None:
    """Frontend humanizer maps `decision`→`Decisions`,
    `observation`→`Observations`, etc. Backend must emit these exact
    keys for the mapping to match — this asserts the contract."""
    facts = [
        _make_fact(fact_id=f"f-{t}", fact_type=t)
        for t in [
            "decision",
            "observation",
            "open_question",
            "action_item",
            "opinion",
        ]
    ]
    out = key_facts.build_key_facts_data(facts)
    types = sorted({it["fact_type"] for it in out["items"]})
    assert types == [
        "action_item",
        "decision",
        "observation",
        "open_question",
        "opinion",
    ]


# ---------------------------------------------------------------------------
# (f) URL hyperlinking — backend preserves raw URLs in body so the
#     frontend's regex-based linkifier can detect them.
# ---------------------------------------------------------------------------


def test_build_key_facts_data_preserves_urls_in_body_for_frontend_linkify() -> None:
    facts = [
        _make_fact(
            memory_text=(
                "Reference doc at https://example.com/spec details the "
                "rotation policy. See also http://example.org/rfc-7519."
            )
        )
    ]
    item = key_facts.build_key_facts_data(facts)["items"][0]
    # Both URLs survive intact in the body; the frontend's linkifier
    # detects + wraps them at render time.
    assert "https://example.com/spec" in item["body"]
    assert "http://example.org/rfc-7519" in item["body"]


# ---------------------------------------------------------------------------
# Robustness — builder is total over malformed input
# ---------------------------------------------------------------------------


def test_build_key_facts_data_total_over_garbage_input() -> None:
    """The builder must never raise — empty payload is the safe
    fallback when the upstream gather step produced nothing useful."""
    for garbage in [
        None,
        [],
        [None, "string", 42],
        [{"memory_text": None, "importance": object()}],
    ]:
        out = key_facts.build_key_facts_data(garbage)  # type: ignore[arg-type]
        assert out["renderer_kind"] == "frontend"
        assert isinstance(out["items"], list)


# ---------------------------------------------------------------------------
# Legacy ``render`` callable still works (catastrophic-fallback path)
# ---------------------------------------------------------------------------


def test_legacy_render_emits_markdown_table_for_fallback_path() -> None:
    """``render()`` is preserved as a fallback for the orchestrator's
    catastrophic path. Returns the same GFM table the v1 module did
    so existing fallback behavior stays intact."""
    out = key_facts.render({"facts": [_make_fact(memory_text="X happened", importance=8)]})
    assert "| Fact" in out
    assert "X happened" in out
