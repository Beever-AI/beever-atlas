"""Tests for the heuristic tension detector — Phase 4 wiki redesign.

The detector is deliberately conservative: false positives are more
visible than false negatives (a stray callout looks like a bug; a
missed tension is unnoticed). These tests pin the conservatism so
future tuning is intentional, not accidental.
"""

from __future__ import annotations

from beever_atlas.wiki.modules.tension_detector import detect_tensions


def _opinion_fact(
    *,
    fact_id: str = "f1",
    text: str = "We should adopt option X.",
    author: str = "Alice",
    sentiment: str | None = "positive",
    entity_tags: list[str] | None = None,
    fact_type: str = "opinion",
    ts: str = "2026-04-22T10:00:00Z",
) -> dict:
    return {
        "id": fact_id,
        "memory_text": text,
        "fact_type": fact_type,
        "author_name": author,
        "sentiment": sentiment,
        "entity_tags": list(entity_tags or ["X"]),
        "message_ts": ts,
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_detects_pair_when_sentiments_oppose_with_shared_entity() -> None:
    """A positive vs concerning opinion pair sharing an entity tag MUST
    surface as a tension. Reflects the canonical case the heuristic is
    designed to catch."""
    facts = [
        _opinion_fact(
            fact_id="f1",
            text="Hand-rolled memory store is tuned for our chat ingestion flow.",
            author="Jacky Chan",
            sentiment="positive",
        ),
        _opinion_fact(
            fact_id="f2",
            text="Custom memory will rot — switching to Google Memory Bank.",
            author="Thomas Chong",
            sentiment="concerning",
        ),
    ]
    result = detect_tensions(facts)
    assert len(result["tensions"]) == 1
    t = result["tensions"][0]
    assert t["status"] == "open"
    assert len(t["positions"]) == 2
    authors = {p["author"] for p in t["positions"]}
    assert authors == {"Jacky Chan", "Thomas Chong"}
    fact_ids = {p["fact_id"] for p in t["positions"]}
    assert fact_ids == {"f1", "f2"}
    # Annotations reference each partner.
    assert result["fact_annotations"]["f1"]["contradicts_fact_id"] == "f2"
    assert result["fact_annotations"]["f2"]["contradicts_fact_id"] == "f1"
    # tension_id is shared across the pair.
    assert (
        result["fact_annotations"]["f1"]["tension_id"]
        == result["fact_annotations"]["f2"]["tension_id"]
    )


def test_recommendation_vs_concerning_counts_as_opposing() -> None:
    """The opposing-set covers both ``positive vs concerning`` and
    ``recommendation vs concerning`` — recommendations carry the same
    'pro' polarity as positive opinions for tension purposes."""
    facts = [
        _opinion_fact(
            fact_id="f1",
            sentiment="recommendation",
            text="Recommend migrating to module X this sprint.",
        ),
        _opinion_fact(
            fact_id="f2",
            sentiment="concerning",
            text="Module X has scaling concerns we haven't resolved.",
        ),
    ]
    result = detect_tensions(facts)
    assert len(result["tensions"]) == 1


# ---------------------------------------------------------------------------
# Skip cases — conservative thresholds
# ---------------------------------------------------------------------------


def test_skips_pair_when_sentiments_match() -> None:
    """Two positive opinions are not a tension even if they share an
    entity. The detector requires polarity opposition."""
    facts = [
        _opinion_fact(fact_id="f1", sentiment="positive"),
        _opinion_fact(fact_id="f2", sentiment="positive"),
    ]
    result = detect_tensions(facts)
    assert result["tensions"] == []
    assert result["fact_annotations"] == {}


def test_skips_pair_when_no_shared_entity() -> None:
    """Opposing sentiments on unrelated subjects (no shared entity)
    are not a tension — they are simply two opinions that happen to
    differ in tone."""
    facts = [
        _opinion_fact(fact_id="f1", sentiment="positive", entity_tags=["X"]),
        _opinion_fact(fact_id="f2", sentiment="concerning", entity_tags=["Y"]),
    ]
    result = detect_tensions(facts)
    assert result["tensions"] == []


def test_skips_non_opinion_typed_facts() -> None:
    """Observation / event / question facts cannot be in tension —
    they are factual reports, not contestable positions. Even with
    opposing sentiments, the detector ignores them."""
    facts = [
        _opinion_fact(
            fact_id="f1",
            sentiment="positive",
            fact_type="observation",
        ),
        _opinion_fact(
            fact_id="f2",
            sentiment="concerning",
            fact_type="observation",
        ),
    ]
    result = detect_tensions(facts)
    assert result["tensions"] == []


def test_skips_facts_with_null_sentiment() -> None:
    """Facts without sentiment cannot be polarised. Pre-Phase-3 facts
    (no sentiment field) collapse this branch to False naturally."""
    facts = [
        _opinion_fact(fact_id="f1", sentiment="positive"),
        _opinion_fact(fact_id="f2", sentiment=None),
    ]
    result = detect_tensions(facts)
    assert result["tensions"] == []


def test_returns_empty_when_single_fact() -> None:
    """A cluster with a single opinion fact cannot produce a pair."""
    result = detect_tensions([_opinion_fact()])
    assert result["tensions"] == []
    assert result["fact_annotations"] == {}


def test_returns_empty_for_empty_input() -> None:
    """Empty / None / non-list input collapses safely."""
    assert detect_tensions([]) == {"tensions": [], "fact_annotations": {}}
    assert detect_tensions(None) == {"tensions": [], "fact_annotations": {}}
    # Non-list defends against arbitrary upstream callers.
    assert detect_tensions("not-a-list") == {  # type: ignore[arg-type]
        "tensions": [],
        "fact_annotations": {},
    }


# ---------------------------------------------------------------------------
# Determinism + id stability
# ---------------------------------------------------------------------------


def test_tension_id_stable_across_runs() -> None:
    """Running the detector twice on the same facts MUST produce the
    same ``tension_id``. The id is a hash of the sorted fact-id pair,
    so the result is deterministic — re-runs do not churn persisted
    page data."""
    facts = [
        _opinion_fact(fact_id="f1", sentiment="positive"),
        _opinion_fact(fact_id="f2", sentiment="concerning"),
    ]
    a = detect_tensions(facts)
    b = detect_tensions(facts)
    assert a["tensions"][0]["tension_id"] == b["tensions"][0]["tension_id"]
    # Reordering the input list MUST NOT change the id (sorted hash).
    c = detect_tensions(list(reversed(facts)))
    assert a["tensions"][0]["tension_id"] == c["tensions"][0]["tension_id"]


def test_tension_id_format() -> None:
    """``tension_id`` MUST be ``t_<8 hex chars>`` — the format is
    documented in the AtomicFact field docstring."""
    facts = [
        _opinion_fact(fact_id="f1", sentiment="positive"),
        _opinion_fact(fact_id="f2", sentiment="concerning"),
    ]
    tension_id = detect_tensions(facts)["tensions"][0]["tension_id"]
    assert tension_id.startswith("t_")
    assert len(tension_id) == 10
    assert all(c in "0123456789abcdef" for c in tension_id[2:])


# ---------------------------------------------------------------------------
# Title selection
# ---------------------------------------------------------------------------


def test_title_uses_first_sentence_capped() -> None:
    """The title is the first sentence of the longer-text fact, capped
    at 80 chars with an ellipsis suffix when truncated."""
    long_first_sentence = (
        "The team should adopt the new memory architecture immediately because "
        "the legacy stack is rotting under sustained ingestion load."
    )
    facts = [
        _opinion_fact(
            fact_id="f1",
            sentiment="positive",
            text=long_first_sentence,
        ),
        _opinion_fact(
            fact_id="f2",
            sentiment="concerning",
            text="Short concern.",
        ),
    ]
    title = detect_tensions(facts)["tensions"][0]["title"]
    assert len(title) <= 80
    assert title.endswith("…")
    # Truncation point is before the 80th char (we kept room for the ellipsis).
    assert title.startswith("The team should adopt")


def test_title_is_clean_first_sentence_when_short() -> None:
    """A short first sentence flows through verbatim — no ellipsis."""
    facts = [
        _opinion_fact(
            fact_id="f1",
            sentiment="positive",
            text="Adopt module X. Many followups stand.",
        ),
        _opinion_fact(
            fact_id="f2",
            sentiment="concerning",
            text="X has scaling concerns.",
        ),
    ]
    title = detect_tensions(facts)["tensions"][0]["title"]
    # Longer fact's first sentence wins.
    assert title == "Adopt module X."


def test_strips_safety_markers_from_text() -> None:
    """Title + stance text MUST pass through ``_strip_safety_markers``
    so wrapper tags don't leak to the frontend."""
    facts = [
        _opinion_fact(
            fact_id="f1",
            sentiment="positive",
            text="<untrusted>Adopt module X.</untrusted>",
        ),
        _opinion_fact(
            fact_id="f2",
            sentiment="concerning",
            text="<untrusted>X has concerns.</untrusted>",
        ),
    ]
    t = detect_tensions(facts)["tensions"][0]
    assert "<untrusted>" not in t["title"]
    for p in t["positions"]:
        assert "<untrusted>" not in p["stance"]


# ---------------------------------------------------------------------------
# Multi-tension behavior
# ---------------------------------------------------------------------------


def test_fact_in_multiple_tensions_keeps_first_annotation() -> None:
    """A fact may be in multiple tensions (rare). The annotation map
    records the FIRST tension found — later tensions still surface in
    ``tensions[]`` but don't overwrite the AtomicFact-level annotation
    (keeps the persisted field deterministic)."""
    facts = [
        _opinion_fact(fact_id="f1", sentiment="positive", entity_tags=["X"]),
        _opinion_fact(fact_id="f2", sentiment="concerning", entity_tags=["X"]),
        _opinion_fact(fact_id="f3", sentiment="concerning", entity_tags=["X"]),
    ]
    result = detect_tensions(facts)
    # f1 is paired with both f2 and f3 (positive vs concerning each).
    assert len(result["tensions"]) == 2
    # f1's annotation records f2 (first detected pair) — f3 still
    # gets its own annotation pointing back to f1.
    assert result["fact_annotations"]["f1"]["contradicts_fact_id"] == "f2"
    assert result["fact_annotations"]["f2"]["contradicts_fact_id"] == "f1"
    assert result["fact_annotations"]["f3"]["contradicts_fact_id"] == "f1"


# ---------------------------------------------------------------------------
# Since date — earlier of the two ts
# ---------------------------------------------------------------------------


def test_since_picks_earlier_iso_date() -> None:
    """``since`` is the earlier of the two contributing ts values,
    reduced to YYYY-MM-DD. The frontend renders 'Since Apr 22' style."""
    facts = [
        _opinion_fact(
            fact_id="f1",
            sentiment="positive",
            ts="2026-04-25T10:00:00Z",
        ),
        _opinion_fact(
            fact_id="f2",
            sentiment="concerning",
            ts="2026-04-22T10:00:00Z",
        ),
    ]
    t = detect_tensions(facts)["tensions"][0]
    assert t["since"] == "2026-04-22"


def test_handles_missing_message_ts_gracefully() -> None:
    """Missing ts on one or both facts produces a best-effort since
    value (the non-empty side wins) — never raises."""
    facts = [
        _opinion_fact(fact_id="f1", sentiment="positive", ts=""),
        _opinion_fact(
            fact_id="f2",
            sentiment="concerning",
            ts="2026-04-22T10:00:00Z",
        ),
    ]
    t = detect_tensions(facts)["tensions"][0]
    assert t["since"] == "2026-04-22"
