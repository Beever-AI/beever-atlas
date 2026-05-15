"""Tests for the ``cross_cutting_decisions`` module.

Covers:
  - catalog entry shape + folder-archetype + min-decisions predicate
  - filter to ``fact_type == "decision"`` only
  - importance sort (critical > high > medium > low)
  - cap at top-N
  - source_page link carries title + slug
  - safety-marker stripping on decision text
  - first-sentence extraction (with capitalisation)
  - empty / malformed inputs return empty items list

Pure unit tests — no LLM, network, or DB.
"""

from __future__ import annotations

from beever_atlas.wiki.modules import MODULE_CATALOG
from beever_atlas.wiki.modules.cross_cutting_decisions import (
    build_cross_cutting_decisions_data,
)


# ---------------------------------------------------------------------------
# Catalog entry
# ---------------------------------------------------------------------------


def test_cross_cutting_decisions_in_catalog() -> None:
    assert "cross_cutting_decisions" in MODULE_CATALOG
    spec = MODULE_CATALOG["cross_cutting_decisions"]
    assert spec.id == "cross_cutting_decisions"
    assert spec.label == "Cross-cutting decisions"
    assert spec.renderer_kind == "frontend"


def test_cross_cutting_predicate_requires_folder_and_min_2_decisions() -> None:
    spec = MODULE_CATALOG["cross_cutting_decisions"]
    assert spec.eligible({"archetype": "folder", "descendant_decision_count": 2}) is True
    assert spec.eligible({"archetype": "folder", "descendant_decision_count": 5}) is True
    assert spec.eligible({"archetype": "folder", "descendant_decision_count": 1}) is False
    assert spec.eligible({"archetype": "topic", "descendant_decision_count": 5}) is False
    assert spec.eligible({}) is False


# ---------------------------------------------------------------------------
# build_cross_cutting_decisions_data
# ---------------------------------------------------------------------------


def test_build_filters_to_decision_facts_only() -> None:
    descendants = [
        {
            "title": "A",
            "slug": "a",
            "facts": [
                {"fact_type": "decision", "memory_text": "Adopt JWT.", "importance": "high"},
                {"fact_type": "claim", "memory_text": "JWT is good.", "importance": "high"},
                {"fact_type": "question", "memory_text": "What about TTL?", "importance": "high"},
            ],
        },
    ]
    data = build_cross_cutting_decisions_data(descendants)
    assert data["label"] == "Cross-cutting decisions"
    assert data["renderer_kind"] == "frontend"
    assert len(data["items"]) == 1
    assert data["items"][0]["title"] == "Adopt JWT."


def test_build_sorts_by_importance_desc() -> None:
    descendants = [
        {
            "title": "A",
            "slug": "a",
            "facts": [
                {
                    "fact_type": "decision",
                    "memory_text": "Low priority decision.",
                    "importance": "low",
                    "message_ts": "2026-04-01",
                },
                {
                    "fact_type": "decision",
                    "memory_text": "Critical decision.",
                    "importance": "critical",
                    "message_ts": "2026-04-02",
                },
                {
                    "fact_type": "decision",
                    "memory_text": "Medium decision.",
                    "importance": "medium",
                    "message_ts": "2026-04-03",
                },
            ],
        },
    ]
    data = build_cross_cutting_decisions_data(descendants)
    # Sort: critical → medium → low (by importance score DESC)
    titles = [d["title"] for d in data["items"]]
    assert titles[0] == "Critical decision."
    assert titles[1] == "Medium decision."
    assert titles[2] == "Low priority decision."


def test_build_caps_at_top_n() -> None:
    descendants = [
        {
            "title": "A",
            "slug": "a",
            "facts": [
                {
                    "fact_type": "decision",
                    "memory_text": f"Decision number {i}.",
                    "importance": "high",
                }
                for i in range(10)
            ],
        },
    ]
    data = build_cross_cutting_decisions_data(descendants, cap=5)
    assert len(data["items"]) == 5


def test_build_attaches_source_page_link() -> None:
    descendants = [
        {
            "title": "JWT Migration",
            "slug": "jwt-migration",
            "facts": [
                {
                    "fact_type": "decision",
                    "memory_text": "Adopt JWT.",
                    "importance": "high",
                    "author_name": "Alan",
                    "message_ts": "2026-04-15",
                    "fact_id": "f1",
                },
            ],
        },
    ]
    data = build_cross_cutting_decisions_data(descendants)
    item = data["items"][0]
    assert item["source_page"]["title"] == "JWT Migration"
    assert item["source_page"]["slug"] == "jwt-migration"
    assert item["decided_by"] == "Alan"
    assert item["decided_at"] == "2026-04-15"
    assert item["fact_id"] == "f1"


def test_build_strips_safety_markers_from_decision_text() -> None:
    """Untrusted-tag wrappers must NOT leak to the frontend."""
    descendants = [
        {
            "title": "A",
            "slug": "a",
            "facts": [
                {
                    "fact_type": "decision",
                    "memory_text": "<untrusted>Adopt JWT immediately.</untrusted>",
                    "importance": "high",
                },
            ],
        },
    ]
    data = build_cross_cutting_decisions_data(descendants)
    title = data["items"][0]["title"]
    assert "<untrusted>" not in title
    assert "</untrusted>" not in title
    assert "Adopt JWT immediately." in title


def test_build_extracts_first_sentence_capitalized() -> None:
    descendants = [
        {
            "title": "A",
            "slug": "a",
            "facts": [
                {
                    "fact_type": "decision",
                    "memory_text": "adopt JWT today. The rest is rationale.",
                    "importance": "high",
                },
            ],
        },
    ]
    data = build_cross_cutting_decisions_data(descendants)
    title = data["items"][0]["title"]
    assert title.startswith("Adopt")
    assert title.endswith(".")


def test_build_handles_empty_descendants() -> None:
    data = build_cross_cutting_decisions_data([])
    assert data["items"] == []
    assert data["label"] == "Cross-cutting decisions"


def test_build_handles_none_input() -> None:
    data = build_cross_cutting_decisions_data(None)  # type: ignore[arg-type]
    assert data["items"] == []


def test_build_normalises_importance_to_string_label() -> None:
    """Numeric importance scores should normalise to the canonical
    string buckets the frontend uses for colour-coding."""
    descendants = [
        {
            "title": "A",
            "slug": "a",
            "facts": [
                {
                    "fact_type": "decision",
                    "memory_text": "First.",
                    "importance": 9,
                },
                {
                    "fact_type": "decision",
                    "memory_text": "Second.",
                    "importance": 5,
                },
            ],
        },
    ]
    data = build_cross_cutting_decisions_data(descendants)
    importances = [d["importance"] for d in data["items"]]
    # Numeric 9 → critical bucket; 5 → medium bucket.
    assert "critical" in importances
    assert "medium" in importances


def test_build_skips_facts_with_blank_decision_text() -> None:
    descendants = [
        {
            "title": "A",
            "slug": "a",
            "facts": [
                {"fact_type": "decision", "memory_text": "", "importance": "high"},
                {"fact_type": "decision", "memory_text": "  ", "importance": "high"},
                {"fact_type": "decision", "memory_text": "Real one.", "importance": "high"},
            ],
        },
    ]
    data = build_cross_cutting_decisions_data(descendants)
    assert len(data["items"]) == 1
    assert data["items"][0]["title"] == "Real one."
