"""Tests for ``tension_callout`` builder — Phase 4 wiki redesign.

The builder is a thin wrapper over ``detect_tensions`` that selects
the first detected tension and shapes it into the React component
contract. Coverage includes:
  - Happy path: detector finds a pair → builder returns full payload.
  - Empty fallback: no tension detected → builder returns empty shape
    so the React component renders ``null``.
"""

from __future__ import annotations

from beever_atlas.wiki.modules.tension_callout import (
    build_tension_callout_data,
)


def _opinion_fact(
    *,
    fact_id: str,
    sentiment: str,
    author: str,
    text: str,
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
# Happy path
# ---------------------------------------------------------------------------


def test_returns_full_payload_when_tension_detected() -> None:
    facts = [
        _opinion_fact(
            fact_id="f_legacy",
            sentiment="positive",
            author="Jacky Chan",
            text="Hand-rolled is tuned for chat memoir ingestion.",
        ),
        _opinion_fact(
            fact_id="f_replace",
            sentiment="concerning",
            author="Thomas Chong",
            text="Custom memory will rot — Google Memory Bank fits better.",
        ),
    ]
    out = build_tension_callout_data(facts)
    assert out["renderer_kind"] == "frontend"
    assert out["label"] == "Tension"
    assert out["status"] == "open"
    assert out["since"] == "2026-04-22"
    assert out["title"]
    assert out["tension_id"].startswith("t_")
    assert len(out["positions"]) == 2
    fact_ids = {p["fact_id"] for p in out["positions"]}
    assert fact_ids == {"f_legacy", "f_replace"}
    authors = {p["author"] for p in out["positions"]}
    assert authors == {"Jacky Chan", "Thomas Chong"}


# ---------------------------------------------------------------------------
# Empty fallback — defensive against the planner picking the module
# despite the predicate failing.
# ---------------------------------------------------------------------------


def test_returns_empty_payload_when_no_tension() -> None:
    """A cluster with no opposing-sentiment opinion pairs returns the
    empty shape — the React component checks ``title``/``positions``
    and renders ``null``."""
    facts = [
        _opinion_fact(
            fact_id="f1",
            sentiment="positive",
            author="A",
            text="Approve.",
        ),
        _opinion_fact(
            fact_id="f2",
            sentiment="positive",
            author="B",
            text="Also approve.",
        ),
    ]
    out = build_tension_callout_data(facts)
    assert out["renderer_kind"] == "frontend"
    assert out["title"] == ""
    assert out["status"] == "open"
    assert out["positions"] == []
    assert out["tension_id"] == ""


def test_returns_empty_payload_for_empty_facts() -> None:
    out = build_tension_callout_data([])
    assert out["title"] == ""
    assert out["positions"] == []


def test_returns_empty_payload_for_none() -> None:
    out = build_tension_callout_data(None)
    assert out["title"] == ""
    assert out["positions"] == []
