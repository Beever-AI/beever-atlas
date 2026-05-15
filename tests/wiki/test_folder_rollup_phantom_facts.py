"""Tests for ``_rollup_folder_child_phantom_facts`` — the nested-folder
stats rollup that fixes empty top-level folder pages.

Background: when a folder's children are themselves folders (e.g.
``Beever Atlas Project/`` containing ``Development & Integration/``,
``Security & Quality/``, etc.), the leaf-style F2 promotion finds
no ``decision_log`` / ``quote_highlights`` modules on those sub-
folder children — they carry ``folder_stats`` / ``top_contributors``
instead. Without the rollup, the parent's stat strip renders 0/0/0/0
despite the sub-folders containing hundreds of memories.

The helper synthesizes phantom facts from the sub-folder's persisted
module data so the parent's ``build_folder_stats_data`` /
``build_top_contributors_data`` re-aggregate to the same numbers.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.compiler import _rollup_folder_child_phantom_facts
from beever_atlas.wiki.modules.folder_stats import build_folder_stats_data
from beever_atlas.wiki.modules.top_contributors import build_top_contributors_data


def _folder_stats_module(memories: int, decisions: int, questions: int) -> dict[str, Any]:
    """Mirrors the shape ``build_folder_stats_data`` persists."""
    return {
        "id": "folder_stats",
        "data": {
            "label": "Folder stats",
            "renderer_kind": "frontend",
            "stats": [
                {"value": str(memories), "label": "memories"},
                {"value": str(decisions), "label": "decisions"},
                {"value": str(questions), "label": "open questions"},
                {"value": "0", "label": "contributors"},
            ],
            "subpage_count": 5,
        },
    }


def _top_contributors_module(names: list[str]) -> dict[str, Any]:
    return {
        "id": "top_contributors",
        "data": {
            "label": "Top contributors",
            "renderer_kind": "frontend",
            "items": [{"name": n, "contribution_count": 1, "top_pages": []} for n in names],
        },
    }


def test_phantom_facts_match_persisted_counts():
    """Round-trip: feed the phantom facts back into ``build_folder_stats_data``
    and confirm the totals match what the sub-folder originally persisted."""
    modules = [
        _folder_stats_module(memories=328, decisions=15, questions=4),
        _top_contributors_module(["Alan", "Bob", "Cara"]),
    ]
    phantoms = _rollup_folder_child_phantom_facts(modules)

    rolled_up = build_folder_stats_data([{"facts": phantoms}])
    stats_by_label = {s["label"]: int(s["value"]) for s in rolled_up["stats"]}
    assert stats_by_label["memories"] == 328
    assert stats_by_label["decisions"] == 15
    assert stats_by_label["open questions"] == 4
    # All 3 distinct contributor names must round-trip.
    assert stats_by_label["contributors"] == 3


def test_phantom_facts_preserve_distinct_contributors():
    """Distinct contributor count must roll up correctly even when
    the contributor roster is larger than the per-bucket count
    (e.g. 5 contributors but only 3 memories)."""
    modules = [
        _folder_stats_module(memories=3, decisions=1, questions=0),
        _top_contributors_module(["Alan", "Bob", "Cara", "Dale", "Eve"]),
    ]
    phantoms = _rollup_folder_child_phantom_facts(modules)
    rolled_up = build_top_contributors_data([{"facts": phantoms, "title": "x"}])
    rolled_names = sorted(item["name"] for item in rolled_up["items"])
    # All 5 contributors must appear — phantom facts beyond the
    # memory budget are appended with author-only entries.
    assert rolled_names == ["Alan", "Bob", "Cara", "Dale", "Eve"]


def test_phantom_facts_empty_when_no_data():
    """Sub-folder with no persisted folder_stats AND no top_contributors
    must yield an empty list — caller appends safely."""
    assert _rollup_folder_child_phantom_facts([]) == []
    assert _rollup_folder_child_phantom_facts([{"id": "subpage_cards", "data": {}}]) == []


def test_phantom_facts_handle_only_folder_stats():
    """Sub-folder with folder_stats but no top_contributors (rare —
    happens when distinct_contributor_count signal is < 2)."""
    modules = [_folder_stats_module(memories=5, decisions=2, questions=1)]
    phantoms = _rollup_folder_child_phantom_facts(modules)
    assert len(phantoms) == 5
    # All facts have empty author since no top_contributors module.
    assert all(f["author_name"] == "" for f in phantoms)
    # Type buckets correct.
    types = [f["fact_type"] for f in phantoms]
    assert types.count("decision") == 2
    assert types.count("question") == 1
    assert types.count("") == 2


def test_phantom_facts_handle_malformed_stats():
    """Non-int stat values must not crash — clamp to 0."""
    modules = [
        {
            "id": "folder_stats",
            "data": {
                "stats": [
                    {"value": "not a number", "label": "memories"},
                    {"value": None, "label": "decisions"},
                ],
            },
        },
    ]
    assert _rollup_folder_child_phantom_facts(modules) == []


def test_phantom_facts_memory_text_is_blank():
    """Phantom facts must NOT carry memory text — they're numeric
    rollup only and should not pollute quote_highlights or other
    content-rendering modules that read ``memory_text``."""
    modules = [
        _folder_stats_module(memories=3, decisions=1, questions=0),
        _top_contributors_module(["Alan"]),
    ]
    phantoms = _rollup_folder_child_phantom_facts(modules)
    assert all(f["memory_text"] == "" for f in phantoms)
