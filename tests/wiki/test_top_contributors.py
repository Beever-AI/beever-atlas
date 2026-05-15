"""Tests for the ``top_contributors`` module.

Covers:
  - catalog entry shape + folder-archetype predicate
  - sort: contribution_count DESC, then name ASC for stable tie-break
  - top-N cap at the configured value
  - top_pages: per-contributor list capped at 2 entries (by count DESC)
  - empty / malformed inputs return empty items list

Pure unit tests — no LLM, network, or DB.
"""

from __future__ import annotations

from beever_atlas.wiki.modules import MODULE_CATALOG
from beever_atlas.wiki.modules.top_contributors import (
    build_top_contributors_data,
)


# ---------------------------------------------------------------------------
# Catalog entry
# ---------------------------------------------------------------------------


def test_top_contributors_in_catalog() -> None:
    assert "top_contributors" in MODULE_CATALOG
    spec = MODULE_CATALOG["top_contributors"]
    assert spec.id == "top_contributors"
    assert spec.label == "Top contributors"
    assert spec.renderer_kind == "frontend"


def test_top_contributors_predicate_requires_folder_and_min_2_contributors() -> None:
    spec = MODULE_CATALOG["top_contributors"]
    assert spec.eligible({"archetype": "folder", "distinct_contributor_count": 2}) is True
    assert spec.eligible({"archetype": "folder", "distinct_contributor_count": 5}) is True
    # 1 contributor → not enough for a "top" strip
    assert spec.eligible({"archetype": "folder", "distinct_contributor_count": 1}) is False
    # Non-folder archetype → not eligible
    assert spec.eligible({"archetype": "topic", "distinct_contributor_count": 5}) is False
    assert spec.eligible({}) is False


# ---------------------------------------------------------------------------
# build_top_contributors_data
# ---------------------------------------------------------------------------


def test_build_aggregates_contribution_counts() -> None:
    descendants = [
        {
            "title": "JWT Migration",
            "facts": [
                {"author_name": "Alan", "memory_text": "f1"},
                {"author_name": "Alan", "memory_text": "f2"},
                {"author_name": "Bob", "memory_text": "f3"},
            ],
        },
    ]
    data = build_top_contributors_data(descendants)
    assert data["label"] == "Top contributors"
    assert data["renderer_kind"] == "frontend"
    items = {c["name"]: c for c in data["items"]}
    assert items["Alan"]["contribution_count"] == 2
    assert items["Bob"]["contribution_count"] == 1


def test_build_sorts_by_count_then_name() -> None:
    descendants = [
        {
            "title": "A",
            "facts": [
                {"author_name": "Carol", "memory_text": "f"},
                {"author_name": "Alan", "memory_text": "f"},
                {"author_name": "Bob", "memory_text": "f"},
            ],
        },
    ]
    data = build_top_contributors_data(descendants)
    # All three have count=1; tie-break is alphabetical by name.
    names = [c["name"] for c in data["items"]]
    assert names == ["Alan", "Bob", "Carol"]


def test_build_caps_at_top_n() -> None:
    descendants = [
        {
            "title": "A",
            "facts": [{"author_name": f"Person{i}", "memory_text": "f"} for i in range(10)],
        },
    ]
    data = build_top_contributors_data(descendants, cap=5)
    assert len(data["items"]) == 5


def test_build_top_pages_truncates_to_2_entries_per_contributor() -> None:
    descendants = [
        {
            "title": "Page A",
            "facts": [
                {"author_name": "Alan", "memory_text": "x"},
                {"author_name": "Alan", "memory_text": "y"},
                {"author_name": "Alan", "memory_text": "z"},
            ],
        },
        {
            "title": "Page B",
            "facts": [
                {"author_name": "Alan", "memory_text": "w"},
                {"author_name": "Alan", "memory_text": "v"},
            ],
        },
        {
            "title": "Page C",
            "facts": [{"author_name": "Alan", "memory_text": "u"}],
        },
    ]
    data = build_top_contributors_data(descendants)
    alan = data["items"][0]
    # Top 2 pages by count: Page A (3), Page B (2)
    assert len(alan["top_pages"]) == 2
    pages = [(p["title"], p["count"]) for p in alan["top_pages"]]
    assert pages == [("Page A", 3), ("Page B", 2)]


def test_build_skips_blank_authors() -> None:
    descendants = [
        {
            "title": "A",
            "facts": [
                {"author_name": "", "memory_text": "f1"},
                {"author_name": "Alan", "memory_text": "f2"},
            ],
        },
    ]
    data = build_top_contributors_data(descendants)
    assert len(data["items"]) == 1
    assert data["items"][0]["name"] == "Alan"


def test_build_handles_user_name_alias() -> None:
    descendants = [
        {
            "title": "A",
            "facts": [
                {"user_name": "Alan", "memory_text": "f1"},
                {"author_name": "Alan", "memory_text": "f2"},
            ],
        },
    ]
    data = build_top_contributors_data(descendants)
    assert len(data["items"]) == 1
    assert data["items"][0]["contribution_count"] == 2


def test_build_handles_empty_input() -> None:
    data = build_top_contributors_data([])
    assert data["items"] == []
    assert data["label"] == "Top contributors"


def test_build_handles_none_input() -> None:
    data = build_top_contributors_data(None)  # type: ignore[arg-type]
    assert data["items"] == []


def test_build_top_pages_shape() -> None:
    """Each top_pages entry must be a dict with title + count keys."""
    descendants = [
        {
            "title": "JWT Migration",
            "facts": [{"author_name": "Alan", "memory_text": "f"}],
        },
    ]
    data = build_top_contributors_data(descendants)
    alan = data["items"][0]
    assert isinstance(alan["top_pages"], list)
    assert alan["top_pages"][0]["title"] == "JWT Migration"
    assert alan["top_pages"][0]["count"] == 1
