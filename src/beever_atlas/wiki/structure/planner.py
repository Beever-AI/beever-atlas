"""3-stage wiki structure planner.

Pipeline:

  Stage 1 (deterministic, no LLM)
    ``HeuristicCandidates.compute(...)`` proposes candidate folder
    boundaries from prefix similarity / entity overlap / co-citation
    density.

  Stage 2 (single LLM call)
    ``WikiStructurePlanner._invoke_llm(...)`` sends the channel summary
    + condensed cluster index + candidate groups to ``STRUCTURE_PLANNER_PROMPT``
    and parses the JSON response into a ``PlannedStructure``.

  Stage 3 (deterministic repair)
    ``validate_plan(...)`` enforces invariants (no orphan, no cycle,
    depth ≤ 4, slug uniqueness). On any failure, the planner falls
    back to a flat structure (today's behaviour) and emits a
    structured ``wiki_structure_planner_fallback`` warning so the
    operator (and dashboards) see what happened.

Total LLM cost: AT MOST ONE call per planner invocation, regardless
of cluster count. The whole pipeline is best-effort — failures NEVER
block wiki regeneration.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Union

from beever_atlas.wiki.structure.heuristic import (
    HeuristicCandidates,
)
from beever_atlas.wiki.structure.validator import (
    PlanValidationError,
    validate_plan,
)

logger = logging.getLogger(__name__)


@dataclass
class PlannedFolder:
    """One folder in the structure planner's output.

    ``rationale`` carries the LLM's one-line explanation of why these
    children belong together. Surfaced as a hover tooltip in the
    sidebar (deferred to task 7.3) and persisted on the folder page so
    operators can audit the agent's decision.
    """

    slug: str
    title: str
    child_slugs: list[str] = field(default_factory=list)
    rationale: str = ""


# wiki-redesign-gap-fill / Group 5 — split heuristic thresholds (per design D4).
_SPLIT_FACT_THRESHOLD = 50
_SPLIT_INBOUND_EDGE_THRESHOLD = 35
_SPLIT_DEPTH_CAP = 3


def _compute_split_candidates(
    *,
    clusters: list[dict[str, Any]],
    fact_graph: list[tuple[str, str]] | None,
) -> list[str]:
    """Return the slug list of clusters that crossed a split threshold.

    Conservative — defaults to no split when signals are absent. Caller
    decides whether to auto-execute (channel policy ``wiki.auto_split_topics``)
    or surface as a proposal in the structure-planner UI.
    """
    if not clusters:
        return []
    inbound: dict[str, int] = {}
    for src, dst in fact_graph or []:
        if dst:
            inbound[dst] = inbound.get(dst, 0) + 1
    candidates: list[str] = []
    for c in clusters:
        slug = c.get("id") or c.get("slug")
        if not slug:
            continue
        # Reject candidates that already look like sub-topics — depth cap.
        if c.get("depth", 0) >= _SPLIT_DEPTH_CAP - 1:
            continue
        fact_count = int(c.get("fact_count") or c.get("member_count") or 0)
        in_edges = inbound.get(slug, 0)
        if fact_count >= _SPLIT_FACT_THRESHOLD or in_edges >= _SPLIT_INBOUND_EDGE_THRESHOLD:
            candidates.append(slug)
    return candidates


@dataclass
class PlannedStructure:
    """Structure planner output — a flat list of folders + leaves at root.

    ``folders`` may nest in the future (task 7-deferred). For v1, the
    folder graph is two levels deep: root folders containing leaf
    cluster slugs. The renderer interprets ``leaves`` as root-level
    pages alongside the folders.

    Use ``flat()`` to construct a planner-output equivalent for the
    "no folders" fallback case.
    """

    folders: list[PlannedFolder] = field(default_factory=list)
    leaves: list[str] = field(default_factory=list)
    fallback_reason: str | None = None
    """Set to a non-None reason code when the planner failed and
    returned a flat structure as a fallback. Used by callers (the
    builder) for telemetry without parsing log lines."""
    split_candidates: list[str] = field(default_factory=list)
    """wiki-redesign-gap-fill / Group 5 — cluster slugs whose fact density /
    inbound-edge count crosses the threshold for sub-topic splitting.
    Default behavior: surface as a proposal in the structure planner UI.
    When ``wiki.auto_split_topics=true`` is set in channel policy, the
    Builder auto-creates Sub-topic pages with parent_id = topic.page_id."""

    @classmethod
    def flat(cls, cluster_slugs: list[str], *, reason: str | None = None) -> PlannedStructure:
        """Build the degenerate "no folders" structure.

        This is what the planner returns when the LLM fails, when the
        cluster count is below the threshold, or when ``WIKI_FOLDER_PLANNER``
        is OFF — every cluster becomes a root-level leaf.
        """
        return cls(folders=[], leaves=list(cluster_slugs), fallback_reason=reason)


# Type alias for the LLM caller injected into WikiStructurePlanner.
# Returns the LLM's raw text response — the planner parses JSON itself.
# Accepts EITHER a sync callable returning str OR an async callable
# returning str — the planner's ``plan`` method awaits the result
# only when needed.
LlmCallable = Callable[[str], Union[str, Awaitable[str]]]


class WikiStructurePlanner:
    """Heuristic-first / LLM-gated / determinism-repaired planner."""

    # Below this many clusters, folders aren't worth the complexity —
    # planner returns flat. Settings expose this as ``WIKI_MIN_TOPICS_FOR_FOLDERS``.
    DEFAULT_MIN_TOPICS_FOR_FOLDERS = 8

    def __init__(
        self,
        *,
        llm: LlmCallable | None = None,
        min_topics_for_folders: int = DEFAULT_MIN_TOPICS_FOR_FOLDERS,
    ) -> None:
        self._llm = llm
        self._min_topics = min_topics_for_folders

    def plan(
        self,
        *,
        channel_summary: str,
        clusters: list[dict[str, Any]],
        fact_graph: list[tuple[str, str]] | None = None,
    ) -> PlannedStructure:
        """Sync entry point — for sync test fixtures only.

        DEPRECATED for production code. The production caller (WikiBuilder)
        uses ``plan_async`` directly because it already runs inside the
        regenerate's event loop. The sync wrapper exists only so the
        existing ``test_planner.py`` suite (which uses sync lambdas as
        the LLM callable) keeps working without async test boilerplate.

        Calling this from sync code that ALREADY runs inside an event
        loop dispatches to a fresh thread + new loop, which CANNOT
        share connection pools or async context with the original
        loop. If you need the planner from production code, use
        ``plan_async`` instead.
        """
        coro = self.plan_async(
            channel_summary=channel_summary,
            clusters=clusters,
            fact_graph=fact_graph,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — safe to drive the coroutine directly.
            return asyncio.run(coro)
        # We're inside an existing event loop AND the caller used the
        # sync API — this is a programming error, but we handle it
        # gracefully by running on a fresh thread to avoid
        # ``RuntimeError: This event loop is already running``.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as exe:
            return exe.submit(asyncio.run, coro).result()

    async def plan_async(
        self,
        *,
        channel_summary: str,
        clusters: list[dict[str, Any]],
        fact_graph: list[tuple[str, str]] | None = None,
    ) -> PlannedStructure:
        """Run the 3-stage pipeline. Always returns a PlannedStructure.

        Falls back to flat on:
          - cluster count below ``min_topics_for_folders``
          - LLM not configured
          - LLM raises an exception
          - LLM output fails JSON parse
          - validator raises PlanValidationError
        """
        cluster_slugs = [c.get("id", "") for c in clusters if c.get("id")]
        cluster_slug_set = {s for s in cluster_slugs if s}

        if len(cluster_slug_set) < self._min_topics:
            # Sparse channel — flat layout is fine and cheaper.
            return PlannedStructure.flat(sorted(cluster_slug_set), reason="below_min_topics")

        # Stage 1: heuristic candidates
        candidates = HeuristicCandidates.compute(clusters, fact_graph)

        # Stage 2: LLM gate (or skip if no LLM)
        if self._llm is None:
            logger.info(
                "wiki_structure_planner_no_llm channel_clusters=%d candidates=%d",
                len(cluster_slug_set),
                len(candidates.groups),
            )
            return PlannedStructure.flat(sorted(cluster_slug_set), reason="no_llm_configured")

        try:
            raw = await self._invoke_llm(
                channel_summary=channel_summary,
                clusters=clusters,
                candidates=candidates,
            )
        except Exception as exc:  # noqa: BLE001 — LLM exceptions are best-effort
            logger.warning(
                "wiki_structure_planner_fallback reason=llm_exception exc=%s clusters=%d",
                type(exc).__name__,
                len(cluster_slug_set),
            )
            return PlannedStructure.flat(sorted(cluster_slug_set), reason="llm_exception")

        # Parse the LLM JSON. Best-effort: strip code fences if present.
        try:
            parsed = _parse_llm_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "wiki_structure_planner_fallback reason=json_parse exc=%s",
                type(exc).__name__,
            )
            return PlannedStructure.flat(sorted(cluster_slug_set), reason="json_parse")

        plan = _structure_from_dict(parsed)

        # Stage 3: validate. Falls back on any invariant violation.
        try:
            validate_plan(plan, cluster_slug_set)
        except PlanValidationError as exc:
            logger.warning(
                "wiki_structure_planner_fallback reason=%s detail=%s",
                exc.reason,
                exc.detail,
            )
            return PlannedStructure.flat(sorted(cluster_slug_set), reason=exc.reason)

        # wiki-redesign-gap-fill / Group 5 — surface split candidates so
        # the Builder (when channel policy allows auto-split) or the
        # operator UI can act on them. Threshold per design D4:
        # ≥35 inbound edges OR ≥50 facts in a cluster.
        plan.split_candidates = _compute_split_candidates(clusters=clusters, fact_graph=fact_graph)

        return plan

    async def _invoke_llm(
        self,
        *,
        channel_summary: str,
        clusters: list[dict[str, Any]],
        candidates: HeuristicCandidates,
    ) -> str:
        """Build the prompt + call the LLM. Returns raw text response.

        Tolerates both sync and async LLM callables: detects whether
        the result is awaitable and awaits it if so. Lets the planner
        be wired into either the legacy sync provider API or the new
        async one without two separate planner classes.
        """
        from beever_atlas.wiki.prompts import build_structure_planner_prompt

        prompt = build_structure_planner_prompt(
            channel_summary=channel_summary,
            clusters=clusters,
            candidate_groups=candidates.groups,
        )
        assert self._llm is not None
        result = self._llm(prompt)
        if inspect.isawaitable(result):
            return await result
        return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Parse the LLM response; tolerate markdown code fences.

    Handles the common case where Gemini / Claude wraps JSON in
    ```json...``` even when asked not to. Strips one outer fence
    before parsing.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Drop opening fence (with or without language tag).
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        # Drop closing fence.
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object, got {type(parsed).__name__}")
    return parsed


def _structure_from_dict(data: dict[str, Any]) -> PlannedStructure:
    """Convert the parsed LLM JSON to a PlannedStructure.

    Tolerates missing or null ``folders``/``leaves`` keys — both
    default to empty lists. The validator catches the resulting
    "missing clusters" case in stage 3, so we don't need defensive
    checks here.
    """
    folders_raw = data.get("folders") or []
    leaves_raw = data.get("leaves") or []
    folders: list[PlannedFolder] = []
    for f in folders_raw:
        if not isinstance(f, dict):
            continue
        folders.append(
            PlannedFolder(
                slug=str(f.get("slug") or ""),
                title=str(f.get("title") or ""),
                child_slugs=[str(s) for s in (f.get("child_slugs") or []) if s],
                rationale=str(f.get("rationale") or ""),
            )
        )
    leaves = [str(s) for s in leaves_raw if s]
    return PlannedStructure(folders=folders, leaves=leaves)


__all__ = [
    "PlannedFolder",
    "PlannedStructure",
    "WikiStructurePlanner",
    "LlmCallable",
]
