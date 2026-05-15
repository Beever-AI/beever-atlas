"""Module planning support — pure functions over topic data.

Provides the building blocks the orchestrator uses to decide which
modules to render:

  - ``compute_signals``   — pure aggregation of cluster facts /
    decisions / entities / media into the dict the orchestrator
    feeds to the LLM and the validator.
  - ``ModulePlan`` / ``ModulePin`` — typed container for the output
    of validation.
  - ``_validate_plan`` — drops modules whose eligibility predicate
    fails (defensive against the LLM picking modules whose data
    shape doesn't fit) and dedups anchor names.
  - ``_HUMAN_RULES`` — human-readable per-module selection criteria
    rendered into the LLM prompt.

This module deliberately does NOT make any LLM calls itself. The
orchestrator runs ONE unified LLM call (planner + writer in a single
prompt) — see ``orchestrator.compile_topic_page_modular``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from beever_atlas.wiki.modules import MODULE_CATALOG, is_known_module

logger = logging.getLogger(__name__)


_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")


def _is_video_host(url: str) -> bool:
    """Return True iff ``url``'s hostname is a known video-host or its
    subdomain. Hostname-anchored to close the
    ``codeql:py/incomplete-url-substring-sanitization`` finding —
    a substring check ``"youtube.com" in url`` would falsely match
    ``https://attacker.example/?youtube.com``.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _VIDEO_HOSTS)


@dataclass
class ModulePin:
    """A media item placement: ties a media id to a fact id at a slot."""

    media_id: str
    fact_id: str
    slot: str  # "hero" | "inline" | "gallery"


@dataclass
class ModulePlan:
    """The validated planning output — ordered modules + media pins."""

    modules: list[dict[str, Any]] = field(default_factory=list)
    media_pins: list[ModulePin] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.modules

    def to_dict(self) -> dict[str, Any]:
        return {
            "modules": list(self.modules),
            "media_pins": [
                {"media_id": p.media_id, "fact_id": p.fact_id, "slot": p.slot}
                for p in self.media_pins
            ],
        }


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------


def compute_signals(
    *,
    cluster: dict[str, Any],
    decisions: list[dict] | None = None,
    entities: list[dict] | None = None,
    relationships: list[dict] | None = None,
    media: list[dict] | None = None,
    related_topics: list[dict] | None = None,
    open_questions: list[dict] | None = None,
    process_steps: list[dict] | None = None,
    alternatives: list[str] | None = None,
    pros_cons_confidence: float = 0.0,
    glossary: list[dict] | None = None,
    descendants: list[dict] | None = None,
) -> dict[str, Any]:
    """Aggregate cluster signals the planner consults.

    Each signal mirrors a field one or more catalog predicates check.
    Centralising aggregation here keeps the planner prompt + the
    validator + the catalog predicates reading from the same source
    of truth — divergence between "what the LLM was told" and "what
    the validator checks" was the most likely failure mode.
    """
    facts = cluster.get("member_facts") or cluster.get("facts") or []
    fact_count = len(facts) if isinstance(facts, list) else 0

    decisions = decisions or []
    entities = entities or []
    relationships = relationships or []
    media = media or []
    related_topics = related_topics or []
    open_questions = open_questions or []
    process_steps = process_steps or []
    alternatives = alternatives or []
    glossary = glossary or []

    # Event span — the spread (in days) between earliest and latest
    # event-typed facts. ``timeline`` module needs both ≥4 events AND
    # ≥14 days of spread.
    event_facts = [
        f
        for f in facts
        if isinstance(f, dict)
        and (f.get("fact_type") or "").lower() in {"event", "action", "decision"}
    ]
    event_count = len(event_facts)
    dates = sorted([str(f.get("date") or "")[:10] for f in event_facts if f.get("date")])
    if len(dates) >= 2:
        try:
            from datetime import date

            d0 = date.fromisoformat(dates[0])
            d1 = date.fromisoformat(dates[-1])
            event_span_days = (d1 - d0).days
        except (ValueError, TypeError):
            event_span_days = 0
    else:
        event_span_days = 0

    # Edge-shape signals — used by the suppression pass to drop
    # entity diagrams where one pair dominates with synonymous verbs
    # (a relation-extraction failure mode), and to drop flow charts
    # that have no directed connections.
    edge_pair_counts: dict[tuple[str, str], int] = {}
    edge_verbs: set[str] = set()
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        src = str(rel.get("from") or "")
        dst = str(rel.get("to") or "")
        if src and dst:
            key = (src, dst)
            edge_pair_counts[key] = edge_pair_counts.get(key, 0) + 1
        verb = str(rel.get("label") or rel.get("type") or "").strip()
        if verb:
            edge_verbs.add(verb)
    max_edges_between_same_pair = max(edge_pair_counts.values()) if edge_pair_counts else 0
    distinct_edge_verbs = len(edge_verbs)

    # Process-step edges — count of process_steps items that declare a
    # ``to`` field (directed edge). Used to drop flow_chart modules
    # whose nodes are all isolated.
    process_step_edge_count = sum(
        1 for step in process_steps if isinstance(step, dict) and str(step.get("to") or "").strip()
    )

    # Strong-claim authors — distinct authors making opinion / decision
    # / claim-typed facts. Surface for ``quote_highlights`` eligibility.
    strong_claim_types = {"opinion", "claim", "decision", "recommendation"}
    strong_claim_authors = {
        (f.get("author_name") or f.get("user_name") or "")
        for f in facts
        if isinstance(f, dict) and (f.get("fact_type") or "").lower() in strong_claim_types
    }
    strong_claim_authors.discard("")

    # Media bucketing — what media is hero-eligible vs inline vs gallery vs
    # link/pdf/video. Computed once here so the planner doesn't have to
    # rederive types from raw media records.
    media_by_kind = {
        "hero_candidate": [],
        "inline": [],
        "gallery": [],
        "link": [],
        "pdf": [],
        "video": [],
    }
    title_lower = (cluster.get("title") or "").lower()
    for m in media:
        if not isinstance(m, dict):
            continue
        kind = (m.get("kind") or m.get("type") or "").lower()
        url = (m.get("url") or "").lower()
        ref_count = int(m.get("referencing_fact_count", 0))
        alt = (m.get("alt") or m.get("title") or "").lower()
        # Hero candidate: explicit ``is_hero=True`` flag wins (gives the
        # gather step a manual override), OR heuristic — alt overlaps
        # title AND ≥3 facts reference it. The heuristic is loose
        # (substring overlap or shared word with ≥4 chars) so common
        # paraphrases between title and alt still match.
        is_hero_explicit = bool(m.get("is_hero"))
        title_words = {w for w in title_lower.split() if len(w) >= 4}
        alt_words = {w for w in alt.split() if len(w) >= 4}
        shared_words = title_words & alt_words
        heuristic_hero = bool(
            alt
            and title_lower
            and (alt in title_lower or title_lower in alt or len(shared_words) >= 2)
            and ref_count >= 3
        )
        if is_hero_explicit or heuristic_hero:
            media_by_kind["hero_candidate"].append(m)
            continue
        # Type buckets.
        if kind in {"image", "screenshot"} or url.endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp")
        ):
            if m.get("source_fact_id"):
                media_by_kind["inline"].append(m)
            else:
                media_by_kind["gallery"].append(m)
        elif kind == "video" or _is_video_host(url) or url.endswith((".mp4", ".webm")):
            media_by_kind["video"].append(m)
        elif kind == "pdf" or url.endswith(".pdf"):
            media_by_kind["pdf"].append(m)
        elif kind == "link" or url.startswith("http"):
            media_by_kind["link"].append(m)

    # Glossary terms used on the page — counts distinct glossary
    # entries whose term matches at least one fact body. Feeds the
    # ``acronym_legend`` predicate.
    if glossary and isinstance(facts, list):
        from beever_atlas.wiki.modules.acronym_legend import (
            count_glossary_terms_used,
        )

        glossary_terms_used = count_glossary_terms_used(glossary, facts)
    else:
        glossary_terms_used = 0

    # Numeric facts — count of facts whose memory_text contains at
    # least one stat-shaped numeric value (currency, k/M-suffix,
    # comma-grouped int, or plain int ≥ 100). Feeds the ``stat_strip``
    # predicate.
    if isinstance(facts, list):
        from beever_atlas.wiki.modules.stat_strip import count_numeric_facts

        numeric_fact_count = count_numeric_facts(facts)
    else:
        numeric_fact_count = 0

    # Folder-archetype descendant aggregates — feed the folder-specific
    # modules (``folder_stats``, ``top_contributors``,
    # ``cross_cutting_decisions``). When ``descendants`` is empty (i.e.,
    # this is a topic page, not a folder), all three counts collapse to
    # 0 and the predicates fail naturally.
    descendant_fact_count = 0
    descendant_decision_count = 0
    descendant_question_count = 0
    distinct_contributor_set: set[str] = set()
    descendants_list = descendants or []
    for d in descendants_list:
        if not isinstance(d, dict):
            continue
        d_facts = d.get("facts") or []
        if not isinstance(d_facts, list):
            continue
        descendant_fact_count += len(d_facts)
        for df in d_facts:
            if not isinstance(df, dict):
                continue
            ft = str(df.get("fact_type") or "").strip().lower()
            if ft == "decision":
                descendant_decision_count += 1
            elif ft == "question":
                descendant_question_count += 1
            name = str(
                df.get("author_name") or df.get("user_name") or df.get("author") or ""
            ).strip()
            if name:
                distinct_contributor_set.add(name)
    distinct_contributor_count = len(distinct_contributor_set)

    # Phase 4 tension detection — runs at signal-compute time so the
    # planner sees a real count when sentiment-enriched facts are
    # present. The detector is a pure heuristic over the cluster's
    # facts (no LLM call). Pre-Phase-3 facts (no ``sentiment``) can
    # never fire a tension, so legacy data collapses to 0 naturally.
    from beever_atlas.wiki.modules.tension_detector import detect_tensions

    tension_result = detect_tensions(facts if isinstance(facts, list) else [])
    tension_count = len(tension_result.get("tensions", []))

    # Narrative-articles change — count of narrative sections persisted
    # on this cluster (or this page's prior compile). Populated when
    # the v3 prompt's narrative pass succeeds and survives validation;
    # zero on pre-narrative clusters so the predicate fails naturally.
    narrative_sections_seq = cluster.get("narrative_sections") or []
    if isinstance(narrative_sections_seq, list):
        narrative_section_count = len(narrative_sections_seq)
    else:
        narrative_section_count = 0
    # ``person_fact_count`` remains a placeholder — Phase 5 enrichment
    # will populate it. Setting to 0 keeps the archetype derivation
    # total without firing the ``person`` branch today.
    person_fact_count = 0
    media_count = (
        len(media_by_kind["hero_candidate"])
        + len(media_by_kind["inline"])
        + len(media_by_kind["gallery"])
        + len(media_by_kind["link"])
        + len(media_by_kind["pdf"])
        + len(media_by_kind["video"])
    )
    archetype = _derive_archetype(
        fact_count=fact_count,
        decision_count=len(decisions),
        tension_count=tension_count,
        person_fact_count=person_fact_count,
        media_count=media_count,
        child_count=int(cluster.get("child_count", 0)),
    )

    return {
        "title": cluster.get("title") or "",
        "fact_count": fact_count,
        "decision_count": len(decisions),
        "event_count": event_count,
        "event_span_days": event_span_days,
        "alternative_count": len(alternatives),
        "pros_cons_confidence": float(pros_cons_confidence),
        "strong_claim_author_count": len(strong_claim_authors),
        "process_step_count": len(process_steps),
        "entity_count": len(entities),
        "entity_edge_count": len(relationships),
        "max_edges_between_same_pair": max_edges_between_same_pair,
        "distinct_edge_verbs": distinct_edge_verbs,
        "process_step_edge_count": process_step_edge_count,
        "open_question_count": len(open_questions),
        "child_count": int(cluster.get("child_count", 0)),
        "related_topics": related_topics,
        "has_media_hero_candidate": bool(media_by_kind["hero_candidate"]),
        "inline_media_count": len(media_by_kind["inline"]),
        "gallery_media_count": len(media_by_kind["gallery"]),
        "link_media_count": len(media_by_kind["link"]),
        "pdf_media_count": len(media_by_kind["pdf"]),
        "video_media_count": len(media_by_kind["video"]),
        "glossary_terms_used": glossary_terms_used,
        "numeric_fact_count": numeric_fact_count,
        # Archetype-detection signals (Phase 4 prep). ``tension_count``
        # and ``person_fact_count`` are placeholder zeros today; Phase 3
        # extraction enrichment will populate them properly.
        "tension_count": tension_count,
        "person_fact_count": person_fact_count,
        "archetype": archetype,
        # Folder-archetype descendant aggregates — feed
        # ``folder_stats`` / ``top_contributors`` /
        # ``cross_cutting_decisions``. Zero on non-folder pages.
        "descendant_fact_count": descendant_fact_count,
        "descendant_decision_count": descendant_decision_count,
        "descendant_question_count": descendant_question_count,
        "distinct_contributor_count": distinct_contributor_count,
        # Narrative-articles change — count of validated narrative
        # sections persisted on this page. Feeds the
        # ``narrative_article`` module predicate.
        "narrative_section_count": narrative_section_count,
    }


# ---------------------------------------------------------------------------
# Archetype derivation — pure function over scalar signals so the result
# is identical between the orchestrator (which feeds it to the prompt)
# and the validator (which gates module eligibility on it).
#
# Decision threshold (``fact_count <= 16 AND decision_density >= 0.25``)
# was originally tighter (``fc <= 8 AND density >= 0.4``) but missed
# pages like CLA Adoption (9 facts, 1-3 decisions, rest context) and
# SaaS Pricing (12 facts, 2-4 decisions). These ARE Decision-archetype
# pages — they're CENTERED on a decision but include the supporting
# context that produced the decision (alternatives discussed, votes,
# rationale). The new threshold catches the "decision + its
# justification" shape while still excluding sprawling Topic pages
# where one stray decision surfaces among 20+ unrelated facts (those
# stay as Topic and the decision shows up in ``decision_log``).
#
# Future archetypes (``tension``, ``person``, ``resource``, ``folder``)
# are placeholders today — their gating signals always evaluate to 0
# until Phase 3 lands. Adding the elif branches now keeps the routing
# table ready for Phase 4 without touching this function again.
# ---------------------------------------------------------------------------


def _derive_archetype(
    fact_count: int,
    decision_count: int,
    tension_count: int,
    person_fact_count: int,
    media_count: int,
    child_count: int,
) -> str:
    """Classify the topic page into one of six archetypes.

    Never raises — malformed inputs fall back to ``"topic"``. The
    catalog's ``decision_banner`` predicate reads this via
    ``signals["archetype"]``.
    """
    try:
        fc = int(fact_count or 0)
        dc = int(decision_count or 0)
        tc = int(tension_count or 0)
        pfc = int(person_fact_count or 0)
        mc = int(media_count or 0)
        cc = int(child_count or 0)
    except (TypeError, ValueError):
        return "topic"

    # Decision: small-to-mid page (≤16 facts) where the decision is
    # the centerpiece (decision_count / fact_count ≥ 0.25). The
    # threshold was loosened from (≤8, ≥0.4) to catch CLA-style pages
    # where 1-3 decisions sit among 6-15 supporting facts; those are
    # Decision-archetype, not Topic-archetype. Pages above 16 facts
    # are too broad to be "centered on a decision" — they stay as
    # Topic and the decision surfaces via ``decision_log``.
    if dc >= 1 and fc <= 16 and (dc / max(fc, 1)) >= 0.25:
        return "decision"
    # Future archetypes — left as elif placeholders that never fire
    # today (tension_count + person_fact_count are 0 until Phase 3).
    if tc >= 1:
        return "tension"
    if pfc >= 8:
        return "person"
    if fc > 0 and (mc / max(fc, 1)) >= 0.6:
        return "resource"
    if cc >= 2:
        return "folder"
    return "topic"


# ---------------------------------------------------------------------------
# Validator — drops modules whose eligibility predicate fails or whose
# ID is unknown. Logs each rejection so soak telemetry can spot
# persistently-bad picks per module type.
# ---------------------------------------------------------------------------


def _validate_plan(
    raw_plan: dict[str, Any],
    signals: dict[str, Any],
) -> ModulePlan:
    """Validate the LLM's plan output against catalog eligibility.

    Returns a ``ModulePlan`` containing only the modules whose
    eligibility predicate passes. Logs each rejection so soak
    telemetry can spot persistently-bad picks per module type.
    """
    modules_in = raw_plan.get("modules") or []
    if not isinstance(modules_in, list):
        modules_in = []

    plan = ModulePlan()
    seen_anchors: set[str] = set()
    for entry in modules_in:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        anchor = str(entry.get("anchor") or "").strip()
        if not mid:
            continue
        if not is_known_module(mid):
            logger.warning(
                "module_rejected reason=unknown_id module=%s",
                mid,
            )
            continue
        spec = MODULE_CATALOG[mid]
        try:
            eligible = bool(spec.eligible(signals))
        except Exception as exc:  # noqa: BLE001 — predicate must never block
            logger.warning(
                "module_rejected reason=predicate_error module=%s exc=%s",
                mid,
                exc,
            )
            continue
        if not eligible:
            logger.info(
                "module_rejected reason=criteria_unmet module=%s",
                mid,
            )
            continue
        # Dedup anchor (orchestrator assumes anchors are unique).
        if not anchor:
            anchor = mid.replace("_", "-")
        candidate = anchor
        suffix = 2
        while candidate in seen_anchors:
            candidate = f"{anchor}-{suffix}"
            suffix += 1
        seen_anchors.add(candidate)
        plan.modules.append({"id": mid, "anchor": candidate})

    # Media pins — preserve the planner's pinning, but only include
    # pins whose slot is recognised. The frontend renderers handle
    # missing/invalid media ids gracefully.
    pins_in = raw_plan.get("media_pins") or []
    if isinstance(pins_in, list):
        for p in pins_in:
            if not isinstance(p, dict):
                continue
            slot = str(p.get("slot") or "").strip().lower()
            if slot not in {"hero", "inline", "gallery"}:
                continue
            media_id = str(p.get("media_id") or "").strip()
            fact_id = str(p.get("fact_id") or "").strip()
            if not media_id:
                continue
            plan.media_pins.append(ModulePin(media_id=media_id, fact_id=fact_id, slot=slot))

    return plan


# ---------------------------------------------------------------------------
# Per-module human-readable rules — rendered into the unified prompt's
# vocabulary block so the LLM knows the eligibility criteria. Kept in
# sync with the catalog predicates by hand. The
# ``test_human_rules_cover_every_catalog_entry`` regression test
# enforces 1:1 coverage.
# ---------------------------------------------------------------------------

_HUMAN_RULES: dict[str, str] = {
    "hero_summary": "ALWAYS pick when fact_count ≥ 1. MUST be module #1 in your plan — the bold TL;DR + summary lead the page.",
    "narrative_article": "Pick when narrative_section_count ≥ 1. Renders a multi-section explanatory article at the TOP of the page; existing modules render below as Reference & Evidence appendix. The narrative_sections payload is produced by the v3 prompt's narrative pass and persisted with the page.",
    "decision_banner": "Pick when archetype == 'decision' (signals.archetype). MUST be module #2 (right after hero_summary) for Decision-archetype pages — the page is centered on the decision, so it gets a spotlight banner instead of a buried key_facts row.",
    "tension_callout": "Pick when tension_count ≥ 1. Place IMMEDIATELY after hero_summary on Topic pages (module #2). On Decision-archetype pages place it after decision_banner (module #3 — decision_banner stays at #2 because it IS the page subject; tension comes next). Tensions are high-signal contradictions surfaced by the heuristic detector — they belong near the top, never below key_facts.",
    "key_facts": "Pick when fact_count ≥ 5.",
    "decision_log": "Pick when decision_count ≥ 1.",
    "timeline": "Pick when event_count ≥ 4 AND event_span_days ≥ 14.",
    "comparison_matrix": "Pick when alternative_count ≥ 2.",
    "pros_cons": "Pick when pros_cons_confidence ≥ 0.7 (you set this; ≥ 0.7 = explicit trade-off discussion).",
    "quote_highlights": "Pick when strong_claim_author_count ≥ 3.",
    "flow_chart": "Pick when process_step_count ≥ 4. Skipped post-validation if process_step_edge_count == 0 (orphan steps look like noise).",
    "entity_diagram": "Pick when entity_count ≥ 3 AND entity_edge_count ≥ 5. Skipped post-validation if a single (source, target) pair dominates with > 5 edges and only one verb, OR if distinct_edge_verbs < 2 (relation-extraction noise).",
    "open_questions": "Pick when open_question_count ≥ 1.",
    "subpage_cards": "Pick when child_count ≥ 2 — a single child reads better as an inline link, so child_count == 1 is suppressed post-validation.",
    "related_threads": "Pick when at least one related topic has score ≥ 0.4.",
    "media_hero": "Pick when has_media_hero_candidate is true. AT MOST ONE per page.",
    "media_inline": "Pick when inline_media_count ≥ 1. Place each marker adjacent to the paragraph mentioning the source fact.",
    "media_gallery": "Pick when gallery_media_count ≥ 3 (after subtracting hero + inline pins).",
    "link_card": "Pick when link_media_count ≥ 1.",
    "pdf_preview": "Pick when pdf_media_count ≥ 1.",
    "video_embed": "Pick when video_media_count ≥ 1.",
    "stat_strip": "Pick when numeric_fact_count ≥ 3. Place it directly after `hero_summary` (top of page) — the numbers ARE the headline.",
    "acronym_legend": "Pick when glossary_terms_used ≥ 2. Place near the bottom of the page so readers resolve unfamiliar acronyms after the main content.",
    "provenance_drawer": "ALWAYS pick when fact_count ≥ 1. MUST be the LAST module in your plan — gives readers (and LLM agents) a drill-down to the original conversation.",
    "folder_stats": "Pick when archetype == 'folder' AND child_count ≥ 2. MUST be module #3 on folder index pages (right after hero_summary + subpage_cards) — the at-a-glance numbers replace the legacy 'Themes & threads' prose.",
    "top_contributors": "Pick when archetype == 'folder' AND distinct_contributor_count ≥ 2. Renders a chip strip of who's most active across the folder descendants.",
    "cross_cutting_decisions": "Pick when archetype == 'folder' AND descendant_decision_count ≥ 2. Surfaces the most important decisions across the folder with deep-links to the source pages.",
}
