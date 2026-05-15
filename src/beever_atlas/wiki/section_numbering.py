"""Path-based section numbering for the wiki tree.

Replaces the historical 3-level inline counter in ``compiler.py`` with a
recursive path builder that supports arbitrary depth (folder → folder →
folder → leaf, capped at 4 by the structure planner). Each child's
section number is ``<parent>.<sibling_index>`` — root nodes get ``1``,
``2``, … and a node at depth N has a dotted path with N segments.

This module is INTENTIONALLY pure (no I/O, no settings access) so it
can be reused by the compiler's structure-building pass, the planner's
validator, and any test that wants to compute deterministic section
labels without setting up the full wiki pipeline.

Spec: ``openspec/changes/llm-wiki-folder-structure/specs/wiki-folder-tree/spec.md``
(Requirement: Path-based section numbering supports arbitrary depth)
"""

from __future__ import annotations

from typing import Any

# We accept any tree node that exposes mutable ``section_number`` and a
# ``children`` list — both ``WikiPageNode`` (sidebar nav) and the
# domain ``WikiPage`` (full page) satisfy this, as do plain dicts used
# by test fixtures. Typing as ``Any`` instead of a Protocol keeps the
# helper portable across BaseModel and dict inputs without forcing
# callers to declare themselves.


def assign_section_numbers(
    roots: list[Any],
    *,
    base_path: str = "",
) -> None:
    """Assign dotted-path section numbers in-place across the tree.

    Walks ``roots`` in their existing order (caller is responsible for
    sorting siblings — the renumbering preserves whatever order they
    arrive in so the planner's chosen ordering survives). Each root
    gets ``base_path.<index>`` (or just ``<index>`` when ``base_path``
    is empty); each child recursively gets ``<root_path>.<index>``.

    No return value — mutation is the contract because both
    ``WikiPageNode`` (sidebar) and the persistence-side ``WikiPage`` are
    BaseModel instances and section numbers are derived state, not
    user-facing inputs.

    Example::

        # Two roots, the second has 3 children, the second of which has 2 grandchildren.
        assign_section_numbers([root_a, root_b])
        # root_a.section_number == "1"
        # root_b.section_number == "2"
        # root_b.children[0].section_number == "2.1"
        # root_b.children[1].section_number == "2.2"
        # root_b.children[1].children[0].section_number == "2.2.1"

    The function is O(N) in the total node count and uses recursion;
    practical wiki trees stay below 200 nodes and 4 levels of depth so
    Python's default 1000-frame stack limit is never approached.
    """
    for index, node in enumerate(roots, start=1):
        path = f"{base_path}.{index}" if base_path else str(index)
        # Use setattr so plain dicts (test fixtures) and BaseModel
        # instances both accept the assignment without per-type branching.
        if hasattr(node, "section_number"):
            try:
                node.section_number = path  # type: ignore[attr-defined]
            except (AttributeError, ValueError):
                # Frozen / no-setter targets — fall through to dict-style.
                if isinstance(node, dict):
                    node["section_number"] = path
        elif isinstance(node, dict):
            node["section_number"] = path
        children = _children_of(node)
        if children:
            assign_section_numbers(children, base_path=path)


def _children_of(node: Any) -> list[Any]:
    """Return the node's children list (mutable reference) regardless of
    whether ``node`` is a Pydantic BaseModel or a plain dict.

    Returns an empty list (not None) for nodes without children so
    callers can safely iterate the result.
    """
    children = getattr(node, "children", None)
    if children is None and isinstance(node, dict):
        children = node.get("children", [])
    return children or []


def compute_tree_depth(roots: list[Any]) -> int:
    """Return the maximum depth of the tree, where roots are depth 1.

    Used by the planner validator to enforce the depth-4 cap. Returns
    0 for an empty tree.
    """
    if not roots:
        return 0
    max_depth = 1
    for node in roots:
        children = _children_of(node)
        if children:
            child_depth = 1 + compute_tree_depth(children)
            if child_depth > max_depth:
                max_depth = child_depth
    return max_depth


__all__ = ["assign_section_numbers", "compute_tree_depth"]
