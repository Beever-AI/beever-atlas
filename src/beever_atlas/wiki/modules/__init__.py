"""Adaptive wiki page modules — catalog + selection-criteria predicates.

Each module type:
- Has a unique ``id`` used everywhere (planner output, writer markers,
  persistence, frontend dispatcher)
- Has a deterministic renderer (compiler-side) OR a frontend-only
  renderer (media modules)
- Has selection criteria the planner consults to decide whether the
  module is eligible for a given topic's data shape

The catalog is the single source of truth: tests enumerate it, the
planner prompt is built from it, the validator rejects unknown IDs.
Adding a module type requires (a) adding the ``ModuleSpec`` here and
(b) adding the renderer (Python or React) — there is intentionally
no auto-registration via filesystem scan.

See ``openspec/changes/adaptive-wiki-page-content/`` for the spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Union

# Caller-supplied LLM callable contract used by both the planner and
# writer LLM calls. Accepts a prompt string; returns either a string
# (sync provider) or an awaitable string (async provider). Defined
# here as the shared canonical type so the two callsites cannot drift
# apart silently.
LLMCallable = Callable[[str], Union[str, Awaitable[str]]]

# Selection-criteria predicates take the planner's "topic signals"
# dict and return True when the module is eligible. Signals are the
# scalar / aggregate data the compiler computes from the cluster
# before invoking the planner — keeping them pre-computed lets each
# predicate be a cheap arithmetic check (no LLM, no IO).
SelectionPredicate = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class ModuleSpec:
    """One entry in the module catalog."""

    id: str
    """Stable machine ID — used in planner output, writer markers, and
    persistence. NEVER rename without a migration."""

    label: str
    """Human-readable label — appears as the module's H2 heading on
    rendered pages and in the right-side TOC."""

    description: str
    """One-line operator/developer description of what the module
    represents — surfaced in docs and debug telemetry."""

    eligible: SelectionPredicate
    """Returns True when the module is eligible for the given topic
    signals. The planner consults this; the validator re-checks after
    the planner emits its plan."""

    renderer_kind: str
    """``"python"`` (renderer lives in ``beever_atlas.wiki.modules.<id>``
    and is a pure function over module data) or ``"frontend"`` (no
    backend renderer; the marker carries data the React component
    consumes directly)."""


# ---------------------------------------------------------------------------
# Selection predicates — kept as small named functions so error messages
# include the failing predicate name when the validator drops a module.
# ---------------------------------------------------------------------------


def _has_min_facts(n: int) -> SelectionPredicate:
    return lambda s: int(s.get("fact_count", 0)) >= n


def _always_eligible_with_min_facts(n: int) -> SelectionPredicate:
    """Always-eligible variant gated only by a minimum fact count.

    Used by ``hero_summary`` so every page with at least one fact
    gets the summary header. The predicate is named distinctly so
    the validator's logs can tell ``hero_summary`` rejections apart
    from ``key_facts`` (which gates at fact_count ≥ 5)."""
    return lambda s: int(s.get("fact_count", 0)) >= n


def _has_min_decisions(n: int) -> SelectionPredicate:
    return lambda s: int(s.get("decision_count", 0)) >= n


def _is_decision_archetype() -> SelectionPredicate:
    """Eligible only when ``compute_signals`` derived ``archetype`` ==
    "decision". Single-fact, decision-centered pages get the banner;
    pages with one decision among many facts stay on the Topic
    archetype and surface the decision via ``decision_log`` instead."""
    return lambda s: str(s.get("archetype") or "").lower() == "decision"


def _has_timeline(min_events: int, min_span_days: int) -> SelectionPredicate:
    def pred(s: dict[str, Any]) -> bool:
        return (
            int(s.get("event_count", 0)) >= min_events
            and int(s.get("event_span_days", 0)) >= min_span_days
        )

    return pred


def _has_alternatives(n: int) -> SelectionPredicate:
    return lambda s: int(s.get("alternative_count", 0)) >= n


def _has_pros_cons_signal(min_confidence: float) -> SelectionPredicate:
    return lambda s: float(s.get("pros_cons_confidence", 0.0)) >= min_confidence


def _has_quote_authors(n: int) -> SelectionPredicate:
    return lambda s: int(s.get("strong_claim_author_count", 0)) >= n


def _has_process_steps(n: int) -> SelectionPredicate:
    return lambda s: int(s.get("process_step_count", 0)) >= n


def _has_entity_graph(min_entities: int, min_edges: int) -> SelectionPredicate:
    def pred(s: dict[str, Any]) -> bool:
        return (
            int(s.get("entity_count", 0)) >= min_entities
            and int(s.get("entity_edge_count", 0)) >= min_edges
        )

    return pred


def _has_open_questions() -> SelectionPredicate:
    return lambda s: int(s.get("open_question_count", 0)) >= 1


def _has_subpages() -> SelectionPredicate:
    return lambda s: int(s.get("child_count", 0)) >= 1


def _has_related_threads(min_score: float) -> SelectionPredicate:
    def pred(s: dict[str, Any]) -> bool:
        related = s.get("related_topics") or []
        if not isinstance(related, list):
            return False
        return any(isinstance(r, dict) and float(r.get("score", 0.0)) >= min_score for r in related)

    return pred


def _has_media_hero_candidate() -> SelectionPredicate:
    return lambda s: bool(s.get("has_media_hero_candidate", False))


def _has_inline_media() -> SelectionPredicate:
    return lambda s: int(s.get("inline_media_count", 0)) >= 1


def _has_gallery_media() -> SelectionPredicate:
    return lambda s: int(s.get("gallery_media_count", 0)) >= 3


def _has_link_media() -> SelectionPredicate:
    return lambda s: int(s.get("link_media_count", 0)) >= 1


def _has_pdf_media() -> SelectionPredicate:
    return lambda s: int(s.get("pdf_media_count", 0)) >= 1


def _has_video_media() -> SelectionPredicate:
    return lambda s: int(s.get("video_media_count", 0)) >= 1


def _has_min_glossary_terms(n: int) -> SelectionPredicate:
    return lambda s: int(s.get("glossary_terms_used", 0)) >= n


def _has_min_numeric_facts(n: int) -> SelectionPredicate:
    return lambda s: int(s.get("numeric_fact_count", 0)) >= n


def _is_folder_with_min_children(n: int) -> SelectionPredicate:
    """Eligible only when ``compute_signals`` derived ``archetype`` ==
    "folder" AND the folder has at least ``n`` direct children. Used by
    the folder-specific dashboard modules (``folder_stats``,
    ``top_contributors``, ``cross_cutting_decisions``)."""

    def pred(s: dict[str, Any]) -> bool:
        return (
            str(s.get("archetype") or "").lower() == "folder" and int(s.get("child_count", 0)) >= n
        )

    return pred


def _is_folder_with_min_contributors(n: int) -> SelectionPredicate:
    """Eligible only when archetype is ``folder`` AND the descendant
    aggregate has at least ``n`` distinct contributors."""

    def pred(s: dict[str, Any]) -> bool:
        return (
            str(s.get("archetype") or "").lower() == "folder"
            and int(s.get("distinct_contributor_count", 0)) >= n
        )

    return pred


def _is_folder_with_min_decisions(n: int) -> SelectionPredicate:
    """Eligible only when archetype is ``folder`` AND the descendant
    aggregate has at least ``n`` decision-typed facts."""

    def pred(s: dict[str, Any]) -> bool:
        return (
            str(s.get("archetype") or "").lower() == "folder"
            and int(s.get("descendant_decision_count", 0)) >= n
        )

    return pred


def _has_tensions() -> SelectionPredicate:
    """Eligible when the cluster has at least one detected tension.

    The detector (``tension_detector.detect_tensions``) runs at
    ``compute_signals`` time and populates ``signals["tension_count"]``.
    Pre-Phase-3 documents (no ``sentiment`` field on facts) can never
    fire a tension, so this predicate naturally collapses to False on
    legacy data."""
    return lambda s: int(s.get("tension_count", 0)) >= 1


def _has_narrative_sections() -> SelectionPredicate:
    """Eligible when the cluster has at least one validated narrative
    section. The signal ``narrative_section_count`` is populated by
    ``compute_signals`` from ``cluster.get("narrative_sections")``;
    pre-narrative pages collapse to 0 naturally."""
    return lambda s: int(s.get("narrative_section_count", 0)) >= 1


# ---------------------------------------------------------------------------
# The catalog — single source of truth.
# ---------------------------------------------------------------------------

MODULE_CATALOG: dict[str, ModuleSpec] = {
    # ---- Content modules ----
    "hero_summary": ModuleSpec(
        id="hero_summary",
        label="Summary",
        description="Bold TL;DR + 2-3 sentence overview + a compact stat strip showing critical / decision / open-question / tension counts. Always module #1 when fact_count ≥ 1.",
        eligible=_always_eligible_with_min_facts(1),
        renderer_kind="frontend",
    ),
    "narrative_article": ModuleSpec(
        id="narrative_article",
        label="Article",
        description="Multi-section article with explanatory prose, inline citations, and optional supporting visuals. Renders at the top of the page; existing modules render below as Reference & Evidence appendix.",
        eligible=_has_narrative_sections(),
        renderer_kind="frontend",
    ),
    "decision_banner": ModuleSpec(
        id="decision_banner",
        label="Decision",
        description="Spotlight banner for single-decision topic pages — surfaces the chosen decision (capitalized headline + optional body), who decided, when, and (Phase 3) rationale / alternatives rejected / open consequences. Only fires when archetype == 'decision' (the page is centered on the decision, not just one decision among many facts).",
        eligible=_is_decision_archetype(),
        renderer_kind="frontend",
    ),
    "tension_callout": ModuleSpec(
        id="tension_callout",
        label="Tension",
        description="Yellow-bordered callout spotlighting a contradicting position pair within the topic — e.g. one contributor recommends X, another flags X as concerning. Activated when ``tension_count >= 1`` (heuristic detector pairs opinion-typed facts with opposing sentiments and shared entity tags). Place IMMEDIATELY after hero_summary (or after decision_banner on Decision-archetype pages) — tensions are high-signal and belong near the top.",
        eligible=_has_tensions(),
        renderer_kind="frontend",
    ),
    "key_facts": ModuleSpec(
        id="key_facts",
        label="Key Facts",
        description="Severity-grouped card list of the highest-importance facts for the topic, rendered as a frontend component (sorted, collapsible, severity-colored).",
        eligible=_has_min_facts(5),
        renderer_kind="frontend",
    ),
    "decision_log": ModuleSpec(
        id="decision_log",
        label="Decisions",
        description="GFM table of decisions made within this topic with status badges and decision-maker attribution.",
        eligible=_has_min_decisions(1),
        renderer_kind="python",
    ),
    "timeline": ModuleSpec(
        id="timeline",
        label="Timeline",
        description="Ordered timeline of events spanning the topic's active period.",
        eligible=_has_timeline(min_events=4, min_span_days=14),
        renderer_kind="python",
    ),
    "comparison_matrix": ModuleSpec(
        id="comparison_matrix",
        label="Comparison",
        description="Matrix comparing two or more named alternatives across criteria extracted from facts.",
        eligible=_has_alternatives(2),
        renderer_kind="python",
    ),
    "pros_cons": ModuleSpec(
        id="pros_cons",
        label="Pros & Cons",
        description="Two-column trade-off summary when the topic discusses an explicit decision with trade-offs.",
        eligible=_has_pros_cons_signal(min_confidence=0.7),
        renderer_kind="python",
    ),
    "quote_highlights": ModuleSpec(
        id="quote_highlights",
        label="Voices",
        description="Verbatim quotes from contributors with attribution and source-thread links.",
        eligible=_has_quote_authors(3),
        renderer_kind="python",
    ),
    "flow_chart": ModuleSpec(
        id="flow_chart",
        label="Process Flow",
        description="Mermaid flow chart for topics describing a process or pipeline with multiple steps.",
        eligible=_has_process_steps(4),
        renderer_kind="python",
    ),
    "entity_diagram": ModuleSpec(
        id="entity_diagram",
        label="Entity Relationships",
        description="Mermaid graph showing how named entities (people, projects, systems) relate within the topic.",
        eligible=_has_entity_graph(min_entities=3, min_edges=5),
        renderer_kind="python",
    ),
    "open_questions": ModuleSpec(
        id="open_questions",
        label="Open Questions",
        description="Unresolved questions surfaced from the topic, each with a raised-on date.",
        eligible=_has_open_questions(),
        renderer_kind="python",
    ),
    "subpage_cards": ModuleSpec(
        id="subpage_cards",
        label="Pages in this section",
        description="Card grid of direct child pages — only on parent topics that own sub-topics.",
        eligible=_has_subpages(),
        renderer_kind="python",
    ),
    "related_threads": ModuleSpec(
        id="related_threads",
        label="Related",
        description="Up to 5 strongly-related topics (Jaccard ≥ 0.4 on shared entities) with one-line reason per link.",
        eligible=_has_related_threads(min_score=0.4),
        renderer_kind="python",
    ),
    # ---- Media modules ----
    "media_hero": ModuleSpec(
        id="media_hero",
        label="Hero",
        description="Single centerpiece image or video, full-width, top of body — for topics with one obvious primary visual.",
        eligible=_has_media_hero_candidate(),
        renderer_kind="frontend",
    ),
    "media_inline": ModuleSpec(
        id="media_inline",
        label="Inline media",
        description="Image or video pinned to a specific paragraph that discusses its source fact.",
        eligible=_has_inline_media(),
        renderer_kind="frontend",
    ),
    "media_gallery": ModuleSpec(
        id="media_gallery",
        label="Gallery",
        description="Responsive grid of media items not pinned to specific paragraphs.",
        eligible=_has_gallery_media(),
        renderer_kind="frontend",
    ),
    "link_card": ModuleSpec(
        id="link_card",
        label="Linked resource",
        description="Cards for external URLs with favicon + title + summary.",
        eligible=_has_link_media(),
        renderer_kind="frontend",
    ),
    "pdf_preview": ModuleSpec(
        id="pdf_preview",
        label="Document",
        description="PDF attachments rendered with first-page thumbnail + Open button.",
        eligible=_has_pdf_media(),
        renderer_kind="frontend",
    ),
    "video_embed": ModuleSpec(
        id="video_embed",
        label="Video",
        description="Lazy-loaded video embed (YouTube, Vimeo, native file URLs).",
        eligible=_has_video_media(),
        renderer_kind="frontend",
    ),
    # ---- Provenance + reading aids ----
    "stat_strip": ModuleSpec(
        id="stat_strip",
        label="Stats",
        description="Headline cards surfacing numeric values (counts, currencies, k/M-suffixed metrics) detected in fact text. Conservative regex — false positives are worse than misses.",
        eligible=_has_min_numeric_facts(3),
        renderer_kind="frontend",
    ),
    "acronym_legend": ModuleSpec(
        id="acronym_legend",
        label="Terms used",
        description="Compact two-column legend of channel-glossary terms that ACTUALLY appear in this page's facts — definitions inline so readers don't jump to the glossary page.",
        eligible=_has_min_glossary_terms(2),
        renderer_kind="frontend",
    ),
    "provenance_drawer": ModuleSpec(
        id="provenance_drawer",
        label="Source messages",
        description="Always-eligible (≥1 fact) collapsed accordion exposing the source messages each fact came from, with platform deep-links — both humans and LLM agents reading the wiki get a drill-down to the original conversation.",
        eligible=_always_eligible_with_min_facts(1),
        renderer_kind="frontend",
    ),
    # ---- Folder-archetype modules (replace prose 'Themes & threads') ----
    "folder_stats": ModuleSpec(
        id="folder_stats",
        label="Folder stats",
        description="4-card big-number strip aggregating descendant pages: total memories, decisions, open questions, and distinct contributors. Replaces the legacy 'Themes & threads' prose with at-a-glance numbers on folder index pages.",
        eligible=_is_folder_with_min_children(2),
        renderer_kind="frontend",
    ),
    "top_contributors": ModuleSpec(
        id="top_contributors",
        label="Top contributors",
        description="Horizontal strip of contributor chips (avatar/initials, name, contribution count, top page) summarising who's most active across folder descendants. Folder pages only.",
        eligible=_is_folder_with_min_contributors(2),
        renderer_kind="frontend",
    ),
    "cross_cutting_decisions": ModuleSpec(
        id="cross_cutting_decisions",
        label="Cross-cutting decisions",
        description="Vertical list of the highest-importance decisions across descendant pages, each with severity colour, decided_by, decided_at, and a deep-link to the source page. Folder pages only.",
        eligible=_is_folder_with_min_decisions(2),
        renderer_kind="frontend",
    ),
}


def is_known_module(module_id: str) -> bool:
    """Cheap O(1) check used by validator + marker substitution."""
    return module_id in MODULE_CATALOG


def get_module(module_id: str) -> ModuleSpec | None:
    return MODULE_CATALOG.get(module_id)


def list_module_ids() -> list[str]:
    """Stable order — used to render the planner prompt's vocabulary
    block and to enumerate modules in tests."""
    return list(MODULE_CATALOG.keys())


__all__ = [
    "ModuleSpec",
    "SelectionPredicate",
    "LLMCallable",
    "MODULE_CATALOG",
    "is_known_module",
    "get_module",
    "list_module_ids",
]
