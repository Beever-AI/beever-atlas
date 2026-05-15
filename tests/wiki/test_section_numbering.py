"""Tests for the recursive path-based section numbering helper.

Covers the contract from
``openspec/changes/llm-wiki-folder-structure/specs/wiki-folder-tree/spec.md``
(Requirement: Path-based section numbering supports arbitrary depth).
"""

from __future__ import annotations

from beever_atlas.models.domain import WikiPageNode
from beever_atlas.wiki.section_numbering import (
    assign_section_numbers,
    compute_tree_depth,
)


def _make_node(node_id: str, *, children: list[WikiPageNode] | None = None) -> WikiPageNode:
    return WikiPageNode(
        id=node_id,
        title=node_id.title(),
        slug=node_id,
        section_number="",
        children=children or [],
    )


def test_root_only_assignment() -> None:
    """5 root pages get section numbers 1..5 in declared order."""
    roots = [_make_node(f"r{i}") for i in range(5)]
    assign_section_numbers(roots)
    assert [n.section_number for n in roots] == ["1", "2", "3", "4", "5"]


def test_two_level_tree() -> None:
    """A folder at root with 3 children gets `2` then `2.1`, `2.2`, `2.3`."""
    children = [_make_node(f"c{i}") for i in range(3)]
    folder = _make_node("folder", children=children)
    other = _make_node("leaf")
    assign_section_numbers([other, folder])
    assert other.section_number == "1"
    assert folder.section_number == "2"
    assert [c.section_number for c in children] == ["2.1", "2.2", "2.3"]


def test_three_level_nesting() -> None:
    """Verify the example from the spec: root 2 has 3 children, the
    second of which has 2 grandchildren — produces 2, 2.1, 2.2, 2.2.1,
    2.2.2, 2.3."""
    grandchildren = [_make_node("g1"), _make_node("g2")]
    children = [
        _make_node("c1"),
        _make_node("c2", children=grandchildren),
        _make_node("c3"),
    ]
    folder = _make_node("folder", children=children)
    assign_section_numbers([_make_node("first"), folder])
    assert folder.section_number == "2"
    assert [c.section_number for c in children] == ["2.1", "2.2", "2.3"]
    assert [g.section_number for g in grandchildren] == ["2.2.1", "2.2.2"]


def test_four_level_path() -> None:
    """Depth-4 nesting (the practical cap) renders as `1.2.3.4`."""
    leaf = _make_node("leaf")
    l3 = _make_node("l3", children=[leaf])
    l2 = _make_node("l2", children=[l3])
    l1 = _make_node("l1", children=[l2])
    assign_section_numbers([l1])
    # l1=1, l2=1.1, l3=1.1.1, leaf=1.1.1.1 — but iteration uses 1-based
    # indexing per level; with one child per level, every step is "1".
    assert l1.section_number == "1"
    assert l2.section_number == "1.1"
    assert l3.section_number == "1.1.1"
    assert leaf.section_number == "1.1.1.1"


def test_stable_across_runs() -> None:
    """Re-running on the same structure yields identical numbers."""
    children = [_make_node(f"c{i}") for i in range(4)]
    folder = _make_node("folder", children=children)
    roots = [folder, _make_node("other")]
    assign_section_numbers(roots)
    snapshot = [(n.section_number, [c.section_number for c in n.children]) for n in roots]
    # Reset and re-run.
    folder.section_number = ""
    for c in children:
        c.section_number = ""
    roots[1].section_number = ""
    assign_section_numbers(roots)
    assert [(n.section_number, [c.section_number for c in n.children]) for n in roots] == snapshot


def test_empty_tree_renders_empty() -> None:
    """assign_section_numbers on [] is a no-op (no exception)."""
    assign_section_numbers([])  # No raise.


def test_dict_node_compatibility() -> None:
    """Plain dict nodes (test fixtures) accept section_number assignment."""
    tree = [
        {"section_number": "", "children": [{"section_number": "", "children": []}]},
        {"section_number": "", "children": []},
    ]
    assign_section_numbers(tree)
    assert tree[0]["section_number"] == "1"
    assert tree[1]["section_number"] == "2"
    assert tree[0]["children"][0]["section_number"] == "1.1"


def test_compute_tree_depth_empty() -> None:
    assert compute_tree_depth([]) == 0


def test_compute_tree_depth_flat() -> None:
    """5 root-only nodes have depth 1."""
    roots = [_make_node(f"r{i}") for i in range(5)]
    assert compute_tree_depth(roots) == 1


def test_compute_tree_depth_nested() -> None:
    """Verify depth calculation matches the deepest path."""
    deep = _make_node(
        "d1",
        children=[
            _make_node(
                "d2",
                children=[
                    _make_node(
                        "d3",
                        children=[
                            _make_node("d4"),
                        ],
                    ),
                ],
            ),
        ],
    )
    shallow = _make_node("s")
    assert compute_tree_depth([shallow, deep]) == 4
