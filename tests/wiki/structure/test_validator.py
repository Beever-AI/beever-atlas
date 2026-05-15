"""Tests for the structure-planner output validator."""

from __future__ import annotations

import pytest

from beever_atlas.wiki.structure.planner import PlannedFolder, PlannedStructure
from beever_atlas.wiki.structure.validator import (
    PlanValidationError,
    validate_plan,
    MAX_DEPTH,
)


def _plan(folders=None, leaves=None) -> PlannedStructure:
    return PlannedStructure(folders=folders or [], leaves=leaves or [])


def test_valid_flat_structure() -> None:
    """All clusters as leaves, no folders → passes."""
    plan = _plan(leaves=["a", "b", "c"])
    validate_plan(plan, expected_cluster_slugs={"a", "b", "c"})


def test_valid_two_level_tree() -> None:
    """One folder with 2 children + 1 leaf → passes."""
    plan = _plan(
        folders=[PlannedFolder(slug="security", title="Security", child_slugs=["a", "b"])],
        leaves=["c"],
    )
    validate_plan(plan, expected_cluster_slugs={"a", "b", "c"})


def test_orphan_cluster_fails() -> None:
    """Cluster 'c' is in expected set but appears nowhere in plan."""
    plan = _plan(leaves=["a", "b"])
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, expected_cluster_slugs={"a", "b", "c"})
    assert exc.value.reason == "cluster_orphan"


def test_duplicate_cluster_in_two_folders_fails() -> None:
    """Same cluster placed in two folders → cluster_duplicate."""
    plan = _plan(
        folders=[
            PlannedFolder(slug="f1", title="F1", child_slugs=["a", "b"]),
            PlannedFolder(slug="f2", title="F2", child_slugs=["a", "c"]),
        ]
    )
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, expected_cluster_slugs={"a", "b", "c"})
    assert exc.value.reason == "cluster_duplicate"


def test_cluster_in_folder_and_leaves_fails() -> None:
    """Same cluster as both folder child and leaf → cluster_duplicate."""
    plan = _plan(
        folders=[PlannedFolder(slug="f1", title="F1", child_slugs=["a"])],
        leaves=["a"],
    )
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, expected_cluster_slugs={"a"})
    assert exc.value.reason == "cluster_duplicate"


def test_folder_slug_collides_with_cluster_fails() -> None:
    """Folder slug == an existing cluster id → slug_collision."""
    plan = _plan(
        folders=[
            PlannedFolder(slug="a", title="A Folder", child_slugs=["b"]),
        ],
        leaves=[],
    )
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, expected_cluster_slugs={"a", "b"})
    assert exc.value.reason == "slug_collision"


def test_duplicate_folder_slugs_fails() -> None:
    """Two folders with the same slug → folder_slug_duplicate."""
    plan = _plan(
        folders=[
            PlannedFolder(slug="x", title="X1", child_slugs=["a"]),
            PlannedFolder(slug="x", title="X2", child_slugs=["b"]),
        ]
    )
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, expected_cluster_slugs={"a", "b"})
    assert exc.value.reason == "folder_slug_duplicate"


def test_missing_folder_slug_fails() -> None:
    """A folder with no slug → folder_slug_missing."""
    plan = _plan(folders=[PlannedFolder(slug="", title="X", child_slugs=["a"])])
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, expected_cluster_slugs={"a"})
    assert exc.value.reason == "folder_slug_missing"


def test_unknown_cluster_in_plan_fails() -> None:
    """Plan references a cluster that wasn't in the expected set."""
    plan = _plan(leaves=["a", "z-not-real"])
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, expected_cluster_slugs={"a"})
    # Either cluster_unknown OR cluster_duplicate could match; in this
    # case "z-not-real" isn't in expected → cluster_unknown.
    assert exc.value.reason in {"cluster_unknown", "cluster_orphan"}


def test_cycle_in_folder_graph_fails() -> None:
    """Folder A contains folder B, folder B contains folder A → cycle."""
    plan = _plan(
        folders=[
            PlannedFolder(slug="folder-a", title="A", child_slugs=["folder-b"]),
            PlannedFolder(slug="folder-b", title="B", child_slugs=["folder-a", "x"]),
        ],
        leaves=[],
    )
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, expected_cluster_slugs={"x"})
    assert exc.value.reason == "cycle_detected"


def test_depth_exceeded_fails() -> None:
    """Build a 5-level chain of folders (1→2→3→4→5) so depth = 5+1 > MAX_DEPTH."""
    assert MAX_DEPTH == 4
    plan = _plan(
        folders=[
            PlannedFolder(slug="L1", title="L1", child_slugs=["L2"]),
            PlannedFolder(slug="L2", title="L2", child_slugs=["L3"]),
            PlannedFolder(slug="L3", title="L3", child_slugs=["L4"]),
            PlannedFolder(slug="L4", title="L4", child_slugs=["L5"]),
            PlannedFolder(slug="L5", title="L5", child_slugs=["leaf"]),
        ]
    )
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, expected_cluster_slugs={"leaf"})
    assert exc.value.reason == "depth_exceeded"


def test_max_depth_4_is_allowed() -> None:
    """3 folder levels (L1→L2→L3) with leaves at level 4 → depth 4, allowed."""
    plan = _plan(
        folders=[
            PlannedFolder(slug="L1", title="L1", child_slugs=["L2"]),
            PlannedFolder(slug="L2", title="L2", child_slugs=["L3"]),
            PlannedFolder(slug="L3", title="L3", child_slugs=["leaf"]),
        ]
    )
    validate_plan(plan, expected_cluster_slugs={"leaf"})


def test_dict_input_accepted() -> None:
    """Plain-dict plan (no PlannedStructure) also validates."""
    plan = {
        "folders": [{"slug": "f1", "title": "F1", "child_slugs": ["a"]}],
        "leaves": ["b"],
    }
    validate_plan(plan, expected_cluster_slugs={"a", "b"})
