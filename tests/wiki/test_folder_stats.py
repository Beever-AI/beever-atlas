"""Tests for the ``folder_stats`` module.

Covers:
  - catalog entry shape + folder-archetype predicate
  - aggregate counts: memories, decisions, open questions, contributors
  - dedup distinct contributors across descendants
  - empty / malformed inputs return zeros without raising

Pure unit tests — no LLM, network, or DB.
"""

from __future__ import annotations

from beever_atlas.wiki.modules import MODULE_CATALOG
from beever_atlas.wiki.modules.folder_stats import build_folder_stats_data


# ---------------------------------------------------------------------------
# Catalog entry
# ---------------------------------------------------------------------------


def test_folder_stats_in_catalog() -> None:
    assert "folder_stats" in MODULE_CATALOG
    spec = MODULE_CATALOG["folder_stats"]
    assert spec.id == "folder_stats"
    assert spec.label == "Folder stats"
    assert spec.renderer_kind == "frontend"


def test_folder_stats_predicate_requires_folder_archetype() -> None:
    spec = MODULE_CATALOG["folder_stats"]
    # folder + ≥2 children → eligible
    assert spec.eligible({"archetype": "folder", "child_count": 2}) is True
    assert spec.eligible({"archetype": "folder", "child_count": 5}) is True
    # folder but only 1 child → not eligible (singleton folder)
    assert spec.eligible({"archetype": "folder", "child_count": 1}) is False
    # other archetypes → not eligible regardless of child_count
    assert spec.eligible({"archetype": "topic", "child_count": 5}) is False
    assert spec.eligible({"archetype": "decision", "child_count": 5}) is False
    # missing signals → defensive False
    assert spec.eligible({}) is False


# ---------------------------------------------------------------------------
# build_folder_stats_data
# ---------------------------------------------------------------------------


def test_build_aggregates_memories_across_descendants() -> None:
    descendants = [
        {"title": "A", "facts": [{"memory_text": "f1"}, {"memory_text": "f2"}]},
        {"title": "B", "facts": [{"memory_text": "f3"}]},
    ]
    data = build_folder_stats_data(descendants)
    assert data["label"] == "Folder stats"
    assert data["renderer_kind"] == "frontend"
    stats = {s["label"]: s["value"] for s in data["stats"]}
    assert stats["memories"] == "3"
    assert data["subpage_count"] == 2


def test_build_counts_decisions_separately() -> None:
    descendants = [
        {
            "title": "A",
            "facts": [
                {"fact_type": "decision", "memory_text": "Adopt JWT"},
                {"fact_type": "claim", "memory_text": "JWT is faster"},
            ],
        },
        {
            "title": "B",
            "facts": [
                {"fact_type": "decision", "memory_text": "Deprecate SAML"},
            ],
        },
    ]
    data = build_folder_stats_data(descendants)
    stats = {s["label"]: s["value"] for s in data["stats"]}
    assert stats["decisions"] == "2"
    assert stats["memories"] == "3"


def test_build_counts_open_questions() -> None:
    descendants = [
        {
            "title": "A",
            "facts": [
                {"fact_type": "question", "memory_text": "What about TTL?"},
                {"fact_type": "question", "memory_text": "Refresh strategy?"},
                {"fact_type": "claim", "memory_text": "Performance ok"},
            ],
        },
    ]
    data = build_folder_stats_data(descendants)
    stats = {s["label"]: s["value"] for s in data["stats"]}
    assert stats["open questions"] == "2"


def test_build_dedupes_distinct_contributors() -> None:
    """Same author across two descendant pages should count as ONE
    contributor, not two."""
    descendants = [
        {
            "title": "A",
            "facts": [
                {"author_name": "Alan", "memory_text": "f1"},
                {"author_name": "Bob", "memory_text": "f2"},
            ],
        },
        {
            "title": "B",
            "facts": [
                {"author_name": "Alan", "memory_text": "f3"},  # duplicate name
                {"author_name": "Carol", "memory_text": "f4"},
            ],
        },
    ]
    data = build_folder_stats_data(descendants)
    stats = {s["label"]: s["value"] for s in data["stats"]}
    assert stats["contributors"] == "3"  # Alan, Bob, Carol


def test_build_handles_user_name_alias() -> None:
    """``user_name`` should fall back when ``author_name`` is missing —
    different ingestion paths use different keys."""
    descendants = [
        {
            "title": "A",
            "facts": [
                {"user_name": "Alan", "memory_text": "f1"},
                {"author_name": "Alan", "memory_text": "f2"},  # same person
            ],
        },
    ]
    data = build_folder_stats_data(descendants)
    stats = {s["label"]: s["value"] for s in data["stats"]}
    assert stats["contributors"] == "1"


def test_build_handles_empty_descendants() -> None:
    data = build_folder_stats_data([])
    stats = {s["label"]: s["value"] for s in data["stats"]}
    assert stats["memories"] == "0"
    assert stats["decisions"] == "0"
    assert stats["open questions"] == "0"
    assert stats["contributors"] == "0"
    assert data["subpage_count"] == 0


def test_build_handles_none_input() -> None:
    """Defensive: None / non-list input returns zeros, not exception."""
    data = build_folder_stats_data(None)  # type: ignore[arg-type]
    assert len(data["stats"]) == 4
    assert all(s["value"] == "0" for s in data["stats"])


def test_build_handles_malformed_descendant_entries() -> None:
    """A non-dict descendant entry (or one with non-list facts) should
    be skipped, not crash."""
    descendants = [
        "not-a-dict",  # type: ignore[list-item]
        {"title": "A"},  # missing facts
        {"title": "B", "facts": "also-not-a-list"},  # type: ignore[dict-item]
        {"title": "C", "facts": [{"memory_text": "f1"}]},
    ]
    data = build_folder_stats_data(descendants)  # type: ignore[arg-type]
    stats = {s["label"]: s["value"] for s in data["stats"]}
    assert stats["memories"] == "1"  # only the well-formed C contributes


def test_build_emits_four_stat_cards_in_canonical_order() -> None:
    descendants = [
        {"title": "A", "facts": [{"author_name": "X", "memory_text": "f"}]},
    ]
    data = build_folder_stats_data(descendants)
    labels = [s["label"] for s in data["stats"]]
    assert labels == ["memories", "decisions", "open questions", "contributors"]
