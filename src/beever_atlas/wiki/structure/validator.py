"""Determinism repair / validation for structure-planner output.

The LLM is best-effort. The validator enforces hard invariants:

  1. **Every cluster placed exactly once** — no orphans, no duplicates.
     A cluster appearing in two folders or in no folder at all is a
     structural bug we can't safely render.
  2. **Acyclic** — no folder is its own ancestor (the LLM can hallucinate
     a self-reference; cheap to detect).
  3. **Depth ≤ 4** — practical cap from the design doc (Decision 1).
     Trees deeper than that get harder for operators to navigate and
     break the section-numbering aesthetic.
  4. **Slugs unique within the channel** — folder slugs collide with
     existing leaf slugs would cascade into the page store's unique
     index and corrupt the cache.

On any violation, ``validate_plan`` raises ``PlanValidationError`` with
a typed reason. The planner catches it and falls back to a flat
structure (today's behavior), logging the failure reason for telemetry.
"""

from __future__ import annotations


class PlanValidationError(Exception):
    """Raised when a planner-emitted structure fails an invariant.

    ``reason`` is a stable string code suitable for log aggregation
    (``cluster_duplicate``, ``cluster_orphan``, ``depth_exceeded``,
    ``cycle_detected``, ``slug_collision``). ``detail`` is the human-
    readable message for log output and operator-facing toasts.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


# Practical cap on tree depth — see design.md Decision 1.
MAX_DEPTH = 4


# The validator accepts any object with ``folders`` (list of dicts or
# PlannedFolder instances exposing ``slug``, ``child_slugs``) and
# ``leaves`` (list of slug strings). Pydantic objects, dataclasses,
# and plain dicts all work via the ``_read_attr`` helper below — we
# normalize via getattr + dict fallbacks instead of importing a
# concrete planner type so the validator stays independently testable.


def validate_plan(plan: object, expected_cluster_slugs: set[str]) -> None:
    """Run all validation passes; raise ``PlanValidationError`` on the first.

    ``expected_cluster_slugs`` is the set of leaf slugs the planner was
    asked to place — every one MUST appear in either ``folders[*].child_slugs``
    or in ``leaves``, and no slug may appear twice.

    Returns ``None`` on success — mutation-free, side-effect-free.
    """
    folders = _read_attr(plan, "folders") or []
    leaves = list(_read_attr(plan, "leaves") or [])

    # ---- 1) Slug placement uniqueness + completeness -----------------------
    placed: dict[str, str] = {}  # slug → "folder:<slug>" | "leaf"
    folder_slugs: set[str] = set()
    for f in folders:
        f_slug = _read_attr(f, "slug") or ""
        if not f_slug:
            raise PlanValidationError(
                "folder_slug_missing",
                "a folder entry has no slug",
            )
        if f_slug in folder_slugs:
            raise PlanValidationError(
                "folder_slug_duplicate",
                f"folder slug '{f_slug}' appears more than once",
            )
        folder_slugs.add(f_slug)
        if f_slug in expected_cluster_slugs:
            # A folder slug must NOT collide with an existing cluster (leaf)
            # slug — they share the page-id space.
            raise PlanValidationError(
                "slug_collision",
                f"folder slug '{f_slug}' collides with a cluster slug",
            )
        for child in _read_attr(f, "child_slugs") or []:
            if child in placed:
                raise PlanValidationError(
                    "cluster_duplicate",
                    f"cluster '{child}' appears in folder '{f_slug}' and also in '{placed[child]}'",
                )
            placed[child] = f"folder:{f_slug}"

    for slug in leaves:
        if slug in placed:
            raise PlanValidationError(
                "cluster_duplicate",
                f"cluster '{slug}' appears as both a leaf and inside '{placed[slug]}'",
            )
        placed[slug] = "leaf"

    # ---- 2) Every expected cluster placed exactly once ---------------------
    missing = expected_cluster_slugs - placed.keys()
    if missing:
        raise PlanValidationError(
            "cluster_orphan",
            f"clusters not placed: {sorted(missing)}",
        )
    extra = placed.keys() - expected_cluster_slugs - folder_slugs
    if extra:
        raise PlanValidationError(
            "cluster_unknown",
            f"plan references unknown clusters: {sorted(extra)}",
        )

    # ---- 3) Acyclic (folder cannot contain itself, no cycle in folder→folder)
    # When folders nest folders (depth > 2), child_slugs may contain another
    # folder's slug. Build a child-of map and DFS.
    folder_children: dict[str, list[str]] = {}
    for f in folders:
        f_slug = _read_attr(f, "slug") or ""
        children = list(_read_attr(f, "child_slugs") or [])
        folder_children[f_slug] = children

    def _walk(node: str, ancestors: tuple[str, ...]) -> int:
        """Return the depth of subtree rooted at ``node``; raise on cycle."""
        if node in ancestors:
            raise PlanValidationError(
                "cycle_detected",
                f"folder '{node}' is its own ancestor (path: {' → '.join(ancestors + (node,))})",
            )
        children = folder_children.get(node) or []
        if not children:
            return 1
        max_child = 0
        new_path = ancestors + (node,)
        for c in children:
            if c in folder_children:
                d = _walk(c, new_path)
                if d > max_child:
                    max_child = d
        return 1 + max_child

    # ---- 4) Depth cap + cycle detection on every folder ------------------
    # Roots are folders not referenced as children of any other folder.
    # We walk roots first to compute depth; then walk EVERY folder (not
    # just roots) to detect cycles that have no entry point — e.g.,
    # A→B→A makes both A and B "non-root" so the roots loop above
    # would silently skip the whole component. Walking every folder
    # ensures cycles always raise even when isolated from any root.
    referenced_as_child = {c for children in folder_children.values() for c in children}
    roots = [s for s in folder_children if s not in referenced_as_child]
    max_depth = 1  # leaves at root start at depth 1
    for root in roots:
        d = _walk(root, ())
        # Add 1 because the leaf below the deepest folder is one more level.
        if d + 1 > max_depth:
            max_depth = d + 1
    # Cycle-detection sweep: walk every folder that isn't already
    # provably acyclic via the roots pass. Folders not reachable from
    # any root are by definition part of an unreachable cycle (or
    # orphan branch); ``_walk`` raises on any cycle it encounters.
    visited_via_roots: set[str] = set()
    for root in roots:
        _collect_reachable(root, folder_children, visited_via_roots)
    for slug in folder_children:
        if slug not in visited_via_roots:
            # Un-rooted folder — must walk to detect cycle. We don't
            # care about its depth (it's unreachable from a root so it
            # wouldn't contribute to a renderable tree anyway), but we
            # do care that ``_walk`` will raise on a cycle.
            _walk(slug, ())
    if max_depth > MAX_DEPTH:
        raise PlanValidationError(
            "depth_exceeded",
            f"plan depth {max_depth} exceeds maximum {MAX_DEPTH}",
        )


def _collect_reachable(
    node: str,
    folder_children: dict[str, list[str]],
    visited: set[str],
) -> None:
    """DFS that collects all folder slugs reachable from ``node``.

    Used to determine which folders the depth-pass walked vs. which
    are isolated cycles needing a separate cycle-only walk. Recursion
    short-circuits on already-visited nodes so we never spin even if
    the input graph happens to have a cycle reachable from a root —
    the depth ``_walk`` raises first in that case anyway.
    """
    if node in visited:
        return
    visited.add(node)
    for child in folder_children.get(node) or []:
        if child in folder_children:
            _collect_reachable(child, folder_children, visited)


def _read_attr(obj: object, name: str):
    """Read ``name`` from a Pydantic model, dataclass, or plain dict."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


__all__ = ["validate_plan", "PlanValidationError", "MAX_DEPTH"]
