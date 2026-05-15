"""Wiki structure planner — decides folder boundaries for a channel's
wiki tree before page synthesis.

This package implements the heuristic-first / LLM-gated / determinism-
repair pipeline described in
``openspec/changes/llm-wiki-folder-structure/design.md`` (Decision 1).

Public surface:

  - ``WikiStructurePlanner.plan(channel_summary, clusters, fact_graph)``
    returns a ``PlannedStructure`` (folders + leaves) representing the
    proposed tree. Falls back to a flat structure on any LLM/validator
    failure so the wiki regen pipeline is never blocked by planning.

  - ``HeuristicCandidates.compute(clusters, fact_graph)`` returns the
    deterministic candidate folder boundaries from prefix/entity/co-
    citation signals — the prior the LLM gate refines.

  - ``validate_plan(plan, clusters)`` raises ``PlanValidationError`` on
    invalid output (cycle, orphan, depth>4, duplicate placement).

The package is intentionally pure-Python with no global state and no
I/O outside the LLM call inside ``WikiStructurePlanner.plan`` —
makes unit-testing trivial and keeps the structural logic auditable.
"""

from __future__ import annotations

from beever_atlas.wiki.structure.heuristic import (
    HeuristicCandidates,
    HeuristicGroup,
)
from beever_atlas.wiki.structure.planner import (
    PlannedFolder,
    PlannedStructure,
    WikiStructurePlanner,
)
from beever_atlas.wiki.structure.validator import (
    PlanValidationError,
    validate_plan,
)

__all__ = [
    "HeuristicCandidates",
    "HeuristicGroup",
    "PlannedFolder",
    "PlannedStructure",
    "WikiStructurePlanner",
    "PlanValidationError",
    "validate_plan",
]
