"""Module-aware topic page orchestrator (single-call architecture).

One LLM call per topic page produces:
  - the module plan (which modules to render, in what order)
  - the TL;DR sentence
  - the Overview prose
  - the body markdown with module markers in place

The validator runs post-response and drops modules whose eligibility
predicates aren't satisfied (defensive against the LLM picking
modules whose data shape doesn't fit). Each surviving module is
rendered deterministically by its Python renderer; the marker
substitution pass splices the rendered content into the body.

Cost: 1 LLM call per topic page — same as today's monolithic
``TOPIC_PROMPT`` flow. Quality bet: structured plan + selection
rules force the model to think about page shape before writing.

Always returns a renderable result. Any failure (LLM, parse,
substitution) falls back to a minimum plan / empty content rather
than raising.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from beever_atlas.wiki.modules import LLMCallable, MODULE_CATALOG
from beever_atlas.wiki.modules.planner import (
    _HUMAN_RULES,
    ModulePin,
    ModulePlan,
    _validate_plan,
)
from beever_atlas.wiki.render import (
    ModuleSubstitutionError,
    substitute_module_markers,
)

logger = logging.getLogger(__name__)


def _safe_hostname(url: str) -> str:
    """Return the lower-cased hostname of ``url`` or ``""`` on parse
    failure / opaque inputs. Hostname-anchored host-checks close the
    ``codeql:py/incomplete-url-substring-sanitization`` finding
    that ``"youtube.com" in url`` raises (the substring check would
    falsely match ``https://attacker.example/?youtube.com``).
    """
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


@dataclass
class ModularPageOutput:
    """Final output the compiler persists.

    ``content`` is the assembled markdown (TL;DR + Overview prose +
    body with markers substituted). ``summary`` is a 1-2 sentence
    summary suitable for cards. ``modules`` is the persisted plan —
    feeds back into the maintainer for surgical patching.
    """

    content: str
    summary: str
    modules: list[dict[str, Any]] = field(default_factory=list)
    media_pins: list[ModulePin] = field(default_factory=list)
    # Telemetry — surfaces in compiler logs so soak runs can compare
    # cost/quality against the legacy single-prompt flow.
    planner_module_count: int = 0
    rendered_module_count: int = 0
    fell_back: bool = False
    # ``wiki-narrative-articles`` — validated narrative sections
    # produced by the v3 prompt. Empty list means flag was OFF, page
    # is pre-narrative, OR the validator rejected the LLM output and
    # the page falls back to module-only rendering.
    narrative_sections: list[dict[str, Any]] = field(default_factory=list)
    narrative_telemetry: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Media data extractors — frontend-only modules consume structured
# media payloads from ``render_inputs["media"]`` rather than markdown.
# Each extractor pulls items that match its module's selector and
# normalises them to the shape the React renderer expects.
# ---------------------------------------------------------------------------


def _extract_media_for_module(
    module_id: str,
    render_inputs: dict[str, Any],
    media_pins: list[Any],
    *,
    tldr: str = "",
    overview: str = "",
    signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the structured ``data`` payload for a frontend-only module.
    Returns a dict with module-specific keys; the React component
    reads it directly via ``module.data``.

    Most branches here build media payloads. Non-media frontend
    modules (e.g. ``key_facts`` v2) delegate to their own builders so
    each module's data shape stays close to its renderer.

    ``media_pins`` is the planner's ordered pin list (each item has
    ``media_id``, ``fact_id``, ``slot``). ``render_inputs["media"]``
    is the raw list of media records gathered from facts.

    Each module gets the slice it owns:
      - ``key_facts``     → severity-grouped card list (frontend v2)
      - ``hero_summary``  → tldr + summary + highlight counts
      - ``media_hero``    → one item (the hero candidate)
      - ``media_inline``  → all inline-pinned items + their fact_ids
      - ``media_gallery`` → unpinned image/video items
      - ``link_card``     → URL items not classified as image/video/pdf
      - ``pdf_preview``   → PDF items
      - ``video_embed``   → YouTube/Vimeo/native video items
    """
    # Non-media frontend modules — delegate to their per-module builder
    # so each module's data shape stays close to its renderer.
    if module_id == "key_facts":
        from beever_atlas.wiki.modules.key_facts import build_key_facts_data

        return build_key_facts_data(render_inputs.get("facts") or [])
    if module_id == "hero_summary":
        from beever_atlas.wiki.modules.hero_summary import build_hero_summary_data

        return build_hero_summary_data(
            tldr=tldr,
            overview=overview,
            signals=signals or {},
            facts=render_inputs.get("facts") or [],
        )
    if module_id == "provenance_drawer":
        from beever_atlas.wiki.modules.provenance_drawer import (
            build_provenance_drawer_data,
        )

        return build_provenance_drawer_data(render_inputs.get("facts") or [])
    if module_id == "acronym_legend":
        from beever_atlas.wiki.modules.acronym_legend import (
            build_acronym_legend_data,
        )

        return build_acronym_legend_data(
            render_inputs.get("glossary") or [],
            render_inputs.get("facts") or [],
        )
    if module_id == "stat_strip":
        from beever_atlas.wiki.modules.stat_strip import build_stat_strip_data

        return build_stat_strip_data(render_inputs.get("facts") or [])
    if module_id == "decision_banner":
        from beever_atlas.wiki.modules.decision_banner import (
            build_decision_banner_data,
        )

        return build_decision_banner_data(
            render_inputs.get("facts") or [],
            render_inputs.get("member_facts") or [],
        )
    if module_id == "tension_callout":
        from beever_atlas.wiki.modules.tension_callout import (
            build_tension_callout_data,
        )

        # Detector reads facts directly — pass the raw cluster facts
        # (member_facts as fallback) so the callout sees the same
        # data the planner saw via ``signals["tension_count"]``.
        pool = render_inputs.get("facts") or render_inputs.get("member_facts") or []
        return build_tension_callout_data(pool)
    if module_id == "folder_stats":
        from beever_atlas.wiki.modules.folder_stats import (
            build_folder_stats_data,
        )

        return build_folder_stats_data(render_inputs.get("descendants") or [])
    if module_id == "top_contributors":
        from beever_atlas.wiki.modules.top_contributors import (
            build_top_contributors_data,
        )

        return build_top_contributors_data(render_inputs.get("descendants") or [])
    if module_id == "cross_cutting_decisions":
        from beever_atlas.wiki.modules.cross_cutting_decisions import (
            build_cross_cutting_decisions_data,
        )

        return build_cross_cutting_decisions_data(render_inputs.get("descendants") or [])
    if module_id == "narrative_article":
        from beever_atlas.wiki.modules.narrative_article import (
            build_narrative_article_data,
        )

        # Narrative sections live on the cluster payload (passed
        # through render_inputs by the orchestrator after the v3
        # prompt's narrative pass succeeded + survived the
        # validator). Fall back to an empty list when missing so the
        # frontend renders nothing rather than crashing.
        return build_narrative_article_data(
            render_inputs.get("narrative_sections") or [],
            render_inputs.get("facts") or [],
        )

    media = render_inputs.get("media") or []
    if not isinstance(media, list):
        media = []

    pinned_ids: set[str] = set()
    for pin in media_pins:
        mid = getattr(pin, "media_id", None)
        if mid:
            pinned_ids.add(str(mid))

    if module_id == "media_hero":
        for m in media:
            if not isinstance(m, dict):
                continue
            if m.get("is_hero") or m.get("hero_candidate"):
                return {
                    "label": "Hero",
                    "renderer_kind": "frontend",
                    "url": m.get("url", ""),
                    "alt": m.get("alt") or m.get("title") or "",
                    "caption": m.get("caption") or "",
                    "source_author": m.get("author") or "",
                    "source_date": m.get("date") or "",
                    "kind": (m.get("kind") or "image"),
                }
        return {"label": "Hero", "renderer_kind": "frontend", "url": "", "alt": ""}

    if module_id == "media_inline":
        items: list[dict] = []
        for pin in media_pins:
            if getattr(pin, "slot", "") != "inline":
                continue
            mid = getattr(pin, "media_id", "")
            for m in media:
                if isinstance(m, dict) and str(m.get("id", "")) == str(mid):
                    items.append(
                        {
                            "media_id": mid,
                            "url": m.get("url", ""),
                            "alt": m.get("alt") or m.get("title") or "",
                            "caption": m.get("caption") or "",
                            "fact_id": getattr(pin, "fact_id", ""),
                            "kind": (m.get("kind") or "image"),
                        }
                    )
                    break
        return {"label": "Inline media", "renderer_kind": "frontend", "items": items}

    if module_id == "media_gallery":
        items = []
        for m in media:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id", ""))
            if mid in pinned_ids:
                continue  # already pinned as inline
            kind = (m.get("kind") or "").lower()
            url = (m.get("url") or "").lower()
            is_image = kind in {"image", "screenshot"} or url.endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".webp")
            )
            if is_image and not m.get("is_hero"):
                items.append(
                    {
                        "url": m.get("url", ""),
                        "alt": m.get("alt") or m.get("title") or "",
                        "caption": m.get("caption") or "",
                        "kind": "image",
                    }
                )
        return {"label": "Gallery", "renderer_kind": "frontend", "items": items}

    if module_id == "link_card":
        items = []
        for m in media:
            if not isinstance(m, dict):
                continue
            kind = (m.get("kind") or "").lower()
            url = m.get("url") or ""
            host = _safe_hostname(url)
            is_video_host = (
                host == "youtube.com"
                or host.endswith(".youtube.com")
                or host == "youtu.be"
                or host == "vimeo.com"
                or host.endswith(".vimeo.com")
            )
            if kind == "link" or (
                url.startswith("http")
                and not url.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".mp4", ".webm")
                )
                and not is_video_host
            ):
                items.append(
                    {
                        "url": url,
                        "title": m.get("title") or m.get("alt") or url,
                        "description": m.get("description") or m.get("caption") or "",
                        "favicon": m.get("favicon") or "",
                    }
                )
        return {"label": "Linked resource", "renderer_kind": "frontend", "items": items}

    if module_id == "pdf_preview":
        items = []
        for m in media:
            if not isinstance(m, dict):
                continue
            kind = (m.get("kind") or "").lower()
            url = m.get("url") or ""
            if kind == "pdf" or url.lower().endswith(".pdf"):
                items.append(
                    {
                        "url": url,
                        "title": m.get("title") or m.get("alt") or "",
                        "thumbnail_url": m.get("thumbnail_url") or "",
                    }
                )
        return {"label": "Document", "renderer_kind": "frontend", "items": items}

    if module_id == "video_embed":
        items = []
        for m in media:
            if not isinstance(m, dict):
                continue
            url = (m.get("url") or "").lower()
            kind = (m.get("kind") or "").lower()
            video_kind: str | None = None
            host = _safe_hostname(url)
            if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be":
                video_kind = "youtube"
            elif host == "vimeo.com" or host.endswith(".vimeo.com"):
                video_kind = "vimeo"
            elif kind == "video" or url.endswith((".mp4", ".webm")):
                video_kind = "native"
            if video_kind:
                items.append(
                    {
                        "url": m.get("url", ""),
                        "kind": video_kind,
                        "title": m.get("title") or m.get("alt") or "",
                    }
                )
        return {"label": "Video", "renderer_kind": "frontend", "items": items}

    return {"label": module_id, "renderer_kind": "frontend"}


# Render-inputs contract.
#
# When the WikiCompiler wires `compile_topic_page_modular` into its
# topic-page path, the gather step MUST populate every key listed
# below that corresponds to a module the planner is allowed to pick.
# Missing keys silently produce empty modules — the planner picks the
# module because the signals said the data exists, the renderer gets
# an empty list, and the rendered markdown is `""`. The substitution
# pass then drops the marker, so the user sees an absent module that
# the planner selected. Hard to debug from page output alone.
#
# Keys, by module they feed:
#   - facts            → key_facts
#   - decisions        → decision_log
#   - events           → timeline
#   - alternatives     → comparison_matrix (also: criteria)
#   - criteria         → comparison_matrix (also: alternatives)
#   - pros, cons       → pros_cons
#   - quotes           → quote_highlights
#   - process_steps    → flow_chart (also: process_edges)
#   - process_edges    → flow_chart (also: process_steps)
#   - entities         → entity_diagram (also: relationships)
#   - relationships    → entity_diagram (also: entities)
#   - open_questions   → open_questions
#   - children         → subpage_cards
#   - related_topics   → related_threads
RENDER_INPUT_KEYS: dict[str, tuple[str, ...]] = {
    "key_facts": ("facts",),
    "decision_log": ("decisions",),
    "timeline": ("events",),
    "comparison_matrix": ("alternatives", "criteria"),
    "pros_cons": ("pros", "cons"),
    "quote_highlights": ("quotes",),
    "flow_chart": ("process_steps", "process_edges"),
    "entity_diagram": ("entities", "relationships"),
    "open_questions": ("open_questions",),
    "subpage_cards": ("children",),
    "related_threads": ("related_topics",),
}


def _per_module_data(module_id: str, render_inputs: dict[str, Any]) -> dict[str, Any]:
    """Pluck the per-module data the deterministic renderer expects
    from the orchestrator's ``render_inputs`` bag. The bag is built
    once by the orchestrator from the gathered cluster data — each
    module gets the slice it needs.

    Keeping this lookup table here (not in each module's render.py)
    means the modules stay decoupled from the compiler's internal
    data model — they only know their own input contract.

    The expected keys per module are documented in ``RENDER_INPUT_KEYS``
    above so the compiler's gather step has a single reference.
    """
    inputs_by_module: dict[str, dict[str, Any]] = {
        "key_facts": {"facts": render_inputs.get("facts", [])},
        "decision_log": {"decisions": render_inputs.get("decisions", [])},
        "timeline": {"events": render_inputs.get("events", [])},
        "comparison_matrix": {
            "alternatives": render_inputs.get("alternatives", []),
            "criteria": render_inputs.get("criteria", []),
        },
        "pros_cons": {
            "pros": render_inputs.get("pros", []),
            "cons": render_inputs.get("cons", []),
        },
        "quote_highlights": {"quotes": render_inputs.get("quotes", [])},
        "flow_chart": {
            "steps": render_inputs.get("process_steps", []),
            "edges": render_inputs.get("process_edges", []),
        },
        "entity_diagram": {
            "entities": render_inputs.get("entities", []),
            "relationships": render_inputs.get("relationships", []),
        },
        "open_questions": {"questions": render_inputs.get("open_questions", [])},
        "subpage_cards": {"children": render_inputs.get("children", [])},
        "related_threads": {"related": render_inputs.get("related_topics", [])},
    }
    return inputs_by_module.get(module_id, {})


def _render_python_module(module_id: str, data: dict[str, Any]) -> str:
    """Import + invoke the per-module Python renderer. Modules with
    ``renderer_kind == "frontend"`` (media modules) return an empty
    string here — the marker substitution drops them silently and
    the frontend renderer takes over via ``page.modules`` data.
    """
    spec = MODULE_CATALOG.get(module_id)
    if spec is None or spec.renderer_kind != "python":
        return ""
    try:
        mod = importlib.import_module(f"beever_atlas.wiki.modules.{module_id}")
    except ImportError as exc:
        logger.warning("module_renderer_import_failed module=%s exc=%s", module_id, exc)
        return ""
    renderer = getattr(mod, "render", None)
    if not callable(renderer):
        logger.warning("module_renderer_missing module=%s", module_id)
        return ""
    try:
        out = renderer(data)
    except Exception as exc:  # noqa: BLE001 — never abort the page on one bad module
        logger.warning(
            "module_renderer_failed module=%s exc_type=%s exc=%s",
            module_id,
            type(exc).__name__,
            exc,
        )
        return ""
    return out if isinstance(out, str) else ""


def _assemble_content(tldr: str, overview: str, substituted_body: str) -> str:
    """Produce the final markdown the page is persisted with.

    Order: TL;DR (bold sentence) → blank line → Overview prose →
    blank line → body with substituted modules. Empty pieces are
    skipped so the result stays clean even when the LLM fell back.
    """
    parts: list[str] = []
    if tldr:
        parts.append(tldr if "**" in tldr else f"**{tldr.strip()}**")
    if overview:
        parts.append(overview.strip())
    if substituted_body:
        parts.append(substituted_body.strip())
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Suppression pass — drops modules whose data shape passes the
# eligibility predicate but whose RENDERED output would be empty or
# noise-only. Runs AFTER ``_validate_plan`` and BEFORE marker
# substitution. Validator checks data SHAPE; this pass checks
# rendered SUBSTANCE.
#
# Rules:
#   1. ``entity_diagram`` noise — one dominant pair (>5 edges) AND
#      only one distinct relation verb across the graph
#   2. ``entity_diagram`` thin — fewer than 2 distinct edge verbs
#      (the same verb everywhere is relation-extraction noise)
#   3. ``flow_chart`` no-edges — process steps have zero ``to`` fields
#   4. ``subpage_cards`` singleton — exactly one child (use inline
#      link instead)
#
# The post-render Mermaid-empty rule lives separately
# (``_suppress_empty_mermaid_modules``) because it requires the
# rendered markdown.
# ---------------------------------------------------------------------------


def _suppress_thin_modules(
    plan: ModulePlan,
    signals: dict[str, Any],
    render_inputs: dict[str, Any],
    *,
    page_id: str = "<unknown>",
) -> ModulePlan:
    """Drop modules whose rendered output would be thin or noise-only.

    Returns a NEW ``ModulePlan`` with the dropped modules removed —
    input is not mutated. Each suppression decision logs a structured
    telemetry line so soak runs can identify persistently-bad picks
    per module type.
    """
    max_pair_edges = int(signals.get("max_edges_between_same_pair", 0))
    distinct_verbs = int(signals.get("distinct_edge_verbs", 0))
    process_edge_count = int(signals.get("process_step_edge_count", 0))
    child_count = int(signals.get("child_count", 0))

    kept: list[dict[str, Any]] = []
    for entry in plan.modules:
        mid = str(entry.get("id") or "")
        reason: str | None = None

        if mid == "entity_diagram":
            # Rule 1 — one dominant pair, only one verb.
            if max_pair_edges > 5 and distinct_verbs <= 1:
                reason = "entity_diagram_dominant_pair_one_verb"
            # Rule 2 — graph-wide verb diversity below threshold.
            elif distinct_verbs < 2:
                reason = "entity_diagram_low_verb_diversity"
        elif mid == "flow_chart":
            # Rule 3 — orphan steps (no directed edges).
            if process_edge_count == 0:
                reason = "flow_chart_no_directed_edges"
        elif mid == "subpage_cards":
            # Rule 4 — singleton parent (zero children handled by
            # predicate; one child reads better as an inline link).
            if child_count == 1:
                reason = "subpage_cards_singleton"

        if reason is not None:
            logger.info(
                "module_suppressed reason=%s module=%s page_id=%s",
                reason,
                mid,
                page_id,
            )
            continue
        kept.append(entry)

    return ModulePlan(modules=kept, media_pins=list(plan.media_pins))


# Matches a fenced ```mermaid block (multiline). Captures the inner
# content so callers can count ``-->`` edges. Conservative: matches
# only when ``mermaid`` is the language tag on the opening fence.
_MERMAID_BLOCK_RE = re.compile(
    r"```mermaid\s*\n(.*?)\n```",
    re.DOTALL,
)


def _mermaid_block_has_no_edges(rendered: str) -> bool:
    """Return True if the rendered markdown's primary output is a
    Mermaid block with zero ``-->`` edges.

    Conservative: only fires when the WRAPPING module's output IS a
    Mermaid block (i.e. the rendered markdown is essentially the
    fenced block). Modules that embed a Mermaid block inside larger
    prose are NOT touched.
    """
    if not isinstance(rendered, str) or "```mermaid" not in rendered:
        return False
    match = _MERMAID_BLOCK_RE.search(rendered)
    if not match:
        return False
    inner = match.group(1)
    # Edge count uses the literal ``-->`` arrow; mermaid's labelled
    # form ``A -->|label| B`` contains ``-->`` so this catches both.
    return "-->" not in inner


# ---------------------------------------------------------------------------
# Body post-substitution cleanup — strips noisy / empty Mermaid blocks
# and empty frontend-only module headers from the substituted body so
# the markdown export does not show graphs / sections that the live
# render path silently suppressed (P3 + P4).
# ---------------------------------------------------------------------------


def _analyze_mermaid_block(inner: str) -> tuple[int, int, int]:
    """Return ``(total_edges, distinct_verbs, max_pair_edges)`` for a
    Mermaid block body.

    ``total_edges`` counts ``-->`` directives. ``distinct_verbs`` counts
    distinct labels emitted with the ``-->|label|`` syntax (zero when
    no labels are present). ``max_pair_edges`` is the maximum count of
    edges sharing the same source/target pair, used to detect "one
    dominant pair with many synonymous verbs" noise patterns the body-
    level prompt is prone to emit.
    """
    edges: list[tuple[str, str, str]] = []
    # Loose grammar — mermaid edges can be ``A --> B`` or ``A -->|verb| B``
    # with an optional label. We don't validate the source / target
    # tokens; any non-whitespace run before / after the arrow counts as
    # a node id for grouping purposes.
    edge_re = re.compile(r"([A-Za-z0-9_]+)\s*-->(?:\s*\|([^|]*)\|)?\s*([A-Za-z0-9_]+)")
    for match in edge_re.finditer(inner):
        src = match.group(1) or ""
        verb = (match.group(2) or "").strip()
        tgt = match.group(3) or ""
        edges.append((src, verb, tgt))

    total_edges = len(edges)
    distinct_verbs = len({v for _, v, _ in edges if v})
    pair_counts: dict[tuple[str, str], int] = {}
    for src, _verb, tgt in edges:
        key = (src, tgt)
        pair_counts[key] = pair_counts.get(key, 0) + 1
    max_pair_edges = max(pair_counts.values(), default=0)
    return total_edges, distinct_verbs, max_pair_edges


def _strip_noisy_body_mermaid(
    body: str,
    *,
    page_id: str = "<unknown>",
) -> str:
    """Post-substitution: scan the body markdown for ```mermaid``` blocks
    and strip the entire block when the graph is noisy.

    Noisy = one of:
      - ``total_edges == 0`` (empty / structureless block)
      - ``max_pair_edges > 5 AND distinct_verbs <= 1`` (one dominant
        pair with synonymous verbs — e.g. USES + INVESTIGATES + OBSERVES
        between the same nodes)

    Mirrors the ``entity_diagram`` MODULE-level suppression rule
    (``_suppress_thin_modules`` rule 1) so body-level Mermaid blocks
    that bypass module suppression get the same treatment. Logs
    structured telemetry for every drop so soak runs can compare drop
    rates module-suppress vs body-suppress.
    """
    if not isinstance(body, str) or "```mermaid" not in body:
        return body

    out_parts: list[str] = []
    cursor = 0
    for match in _MERMAID_BLOCK_RE.finditer(body):
        inner = match.group(1)
        total_edges, distinct_verbs, max_pair_edges = _analyze_mermaid_block(inner)
        is_noisy = total_edges == 0 or (max_pair_edges > 5 and distinct_verbs <= 1)
        if not is_noisy:
            continue
        # Emit pre-block content + drop the whole fenced block. Telemetry
        # carries enough signal to diagnose why we dropped it without
        # re-reading the page.
        reason = "no_edges" if total_edges == 0 else "dominant_pair_one_verb"
        logger.info(
            "body_mermaid_suppressed reason=%s page_id=%s "
            "total_edges=%d distinct_verbs=%d max_pair_edges=%d",
            reason,
            page_id,
            total_edges,
            distinct_verbs,
            max_pair_edges,
        )
        out_parts.append(body[cursor : match.start()])
        cursor = match.end()
    if cursor == 0:
        # No noisy block fired — return original body unchanged so we
        # don't subtly perturb formatting (whitespace, etc.).
        return body
    out_parts.append(body[cursor:])
    # Collapse any triple+ newlines the block removal may have created.
    cleaned = "".join(out_parts)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


# Frontend-only module section headers the v3 prompt is told to leave
# in the body but whose modules are rendered by the React dispatcher
# (NOT by marker substitution). When a frontend-only module's header
# is present in the body but not followed by content, the export shows
# an empty section header — strip them defensively.
_FRONTEND_ONLY_HEADERS: tuple[str, ...] = (
    "Terms Used",
    "Source Messages",
    "Sources Used",
    "Reading aids",
    "Reading Aids",
    "Glossary",
    "Hero",
    "Inline media",
    "Linked resource",
    "Document",
    "Video",
    "Gallery",
)


def _strip_empty_frontend_section_headers(
    body: str,
    *,
    page_id: str = "<unknown>",
) -> str:
    """Post-substitution: drop frontend-only module section headers
    whose body is empty (only whitespace) before the next ``###`` /
    ``##`` heading or end-of-content.

    Limited to the known list of frontend-rendered module labels so we
    don't accidentally strip legitimate empty sections that happen to
    have the same heading text.
    """
    if not isinstance(body, str) or "###" not in body:
        return body

    lines = body.split("\n")
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Match ``### {label}`` (level-3 heading). Leading whitespace OK.
        if stripped.startswith("###"):
            label = stripped.lstrip("#").strip()
            if label in _FRONTEND_ONLY_HEADERS:
                # Look ahead — until we hit another heading (## / ###),
                # a horizontal rule, or end-of-content. If the lookahead
                # window contains only whitespace, drop the header.
                j = i + 1
                has_content = False
                while j < len(lines):
                    nxt = lines[j].strip()
                    if (
                        nxt.startswith("###")
                        or nxt.startswith("## ")
                        or nxt == "##"
                        or nxt.startswith("---")
                    ):
                        break
                    if nxt:
                        has_content = True
                        break
                    j += 1
                if not has_content:
                    logger.info(
                        "body_empty_section_stripped section=%r page_id=%s",
                        label,
                        page_id,
                    )
                    # Skip the heading line + any pure-whitespace lines
                    # up to the next heading / hr.
                    i = j
                    continue
        out_lines.append(line)
        i += 1
    cleaned = "\n".join(out_lines)
    # Collapse any triple+ newlines created by removing a header.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _suppress_empty_mermaid_modules(
    plan: ModulePlan,
    rendered_modules: dict[str, str],
    *,
    page_id: str = "<unknown>",
) -> tuple[ModulePlan, dict[str, str]]:
    """After per-module rendering, drop any module whose rendered
    output is a Mermaid block with zero ``-->`` edges.

    Returns the trimmed plan + the rendered_modules dict with the
    dropped module IDs removed. General by design — any future
    Mermaid-emitting module benefits without code changes.
    """
    kept: list[dict[str, Any]] = []
    rendered_kept: dict[str, str] = dict(rendered_modules)
    for entry in plan.modules:
        mid = str(entry.get("id") or "")
        rendered = rendered_modules.get(mid, "")
        if rendered and _mermaid_block_has_no_edges(rendered):
            logger.info(
                "module_suppressed reason=%s module=%s page_id=%s",
                "mermaid_block_zero_edges",
                mid,
                page_id,
            )
            rendered_kept.pop(mid, None)
            continue
        kept.append(entry)
    return ModulePlan(modules=kept, media_pins=list(plan.media_pins)), rendered_kept


def _parse_compile_json(raw: str) -> dict[str, Any]:
    """Parse the unified prompt's response. Strips a single outer
    markdown fence if the LLM wrapped despite being told not to.
    Validates the top-level shape but DOES NOT enforce the inner
    fields — they're sanity-checked by the caller during validation.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object, got {type(parsed).__name__}")
    return parsed


def _fallback_output(title: str, render_inputs: dict[str, Any]) -> "ModularPageOutput":
    """Catastrophic fallback when the LLM call fails or produces
    unparseable output. Renders a single ``key_facts`` module if
    the data is there, otherwise a placeholder line. The page
    always renders something rather than 404-ing the user.

    ``key_facts`` is a frontend renderer in v2, so the fallback path
    builds the structured payload + a markdown table for the legacy
    page.content body via ``render_key_facts_table`` directly.
    """
    from beever_atlas.wiki.modules.key_facts import build_key_facts_data
    from beever_atlas.wiki.render import render_key_facts_table

    plan = ModulePlan(modules=[{"id": "key_facts", "anchor": "key-facts"}])
    facts = render_inputs.get("facts") or []
    rendered = render_key_facts_table(facts) if isinstance(facts, list) else ""
    if rendered:
        # Attach structured data payload so the frontend dispatcher
        # can render the v2 card list. The markdown body still
        # carries the legacy table so older readers + the page.content
        # cache see something readable.
        plan.modules[0]["data"] = build_key_facts_data(facts)
        content = f"**{title}**\n\n{rendered}"
    else:
        content = f"**{title}** — page content temporarily unavailable. Try a regenerate."
    return ModularPageOutput(
        content=content,
        summary="",
        modules=list(plan.modules),
        planner_module_count=len(plan.modules),
        rendered_module_count=1 if rendered else 0,
        fell_back=True,
    )


async def compile_topic_page_modular(
    *,
    title: str,
    summary: str,
    signals: dict[str, Any],
    render_inputs: dict[str, Any],
    top_facts: list[dict],
    top_people: list[dict],
    date_range_start: str = "",
    date_range_end: str = "",
    llm: LLMCallable,
    channel_config: dict[str, Any] | None = None,
) -> ModularPageOutput:
    """Single-call topic page compilation.

    Builds one prompt containing the catalog + signals + topic data,
    invokes the LLM once, parses the unified response, validates the
    plan, renders modules deterministically, substitutes markers,
    assembles the final content. Returns a renderable
    ``ModularPageOutput`` even on failure (fall-back is single
    ``key_facts`` module).

    Cost: one LLM call per topic page — same as the legacy
    ``TOPIC_PROMPT`` flow.

    ``wiki-narrative-articles`` integration: the v3 prompt is always
    used. The LLM response carries a ``narrative_sections`` array that
    is validated, persisted to ``WikiPage.narrative_sections``, and
    surfaces via the new ``narrative_article`` module. When the
    validator rejects (low coverage / parse error / LLM crash), the
    orchestrator persists ``narrative_sections=[]`` and the page
    renders module-only — that graceful fallback is the safety
    mechanism, no operator-side feature flag.
    """
    from beever_atlas.wiki.prompts import (
        build_module_compile_prompt_v3,
        get_archetype_hint_block,
    )

    catalog_view = [
        {
            "id": spec.id,
            "label": spec.label,
            "description": spec.description,
            "rule": _HUMAN_RULES.get(spec.id, "Pick at planner's discretion."),
        }
        for spec in MODULE_CATALOG.values()
    ]

    # Per-archetype soft hints (Decision 2 in
    # ``openspec/changes/wiki-narrative-articles/design.md``):
    # Decision/Tension/Folder/Channel-Overview archetypes get a
    # suggested section structure; Topic archetype gets the empty
    # string — sections come entirely from cluster content.
    archetype_hint = get_archetype_hint_block(str(signals.get("archetype") or ""))
    prompt = build_module_compile_prompt_v3(
        signals=signals,
        module_catalog=catalog_view,
        title=title,
        summary=summary,
        top_facts=top_facts,
        top_people=top_people,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        archetype_hint_block=archetype_hint,
    )

    # Stage 1 — single LLM call.
    try:
        result = llm(prompt)
        raw = await result if inspect.isawaitable(result) else result  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001
        logger.exception("module_compile_llm_failed exc=%s", exc)
        # Emit ``narrative_article_metrics`` on the LLM-error fallback
        # path so soak dashboards see fallback metrics consistently
        # across every failure cause (LLM crash, parse error, validator
        # rejection).
        page_slug_for_log = str(render_inputs.get("page_id") or title)
        logger.info(
            "narrative_article_fallback reason=llm_error page=%s",
            page_slug_for_log,
        )
        logger.info(
            "narrative_article_metrics page=%s section_count=0 total_words=0 "
            "citation_coverage=0.000 distinct_facts_cited=0 rejected=True "
            "reason=llm_error",
            page_slug_for_log,
        )
        return _fallback_output(title, render_inputs)

    # Stage 2 — parse JSON response.
    try:
        parsed = _parse_compile_json(str(raw))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("module_compile_parse_failed exc=%s raw_len=%d", exc, len(str(raw)))
        # Parse failure means the narrative payload is also lost —
        # surface a structured fallback log line so soak telemetry
        # sees the cause.
        page_slug_for_log = str(render_inputs.get("page_id") or title)
        logger.info(
            "narrative_article_fallback reason=parse_error page=%s",
            page_slug_for_log,
        )
        # H-8: also emit ``narrative_article_metrics`` on the
        # parse-error fallback path so dashboards can aggregate
        # fallback rate consistently with the success path. Without
        # this, the dashboard would see ``rejected=False,
        # section_count=0`` for every parse failure.
        logger.info(
            "narrative_article_metrics page=%s section_count=0 total_words=0 "
            "citation_coverage=0.000 distinct_facts_cited=0 rejected=True "
            "reason=parse_error",
            page_slug_for_log,
        )
        return _fallback_output(title, render_inputs)

    # Stage 2.5 — extract + validate narrative_sections.
    # When the validator rejects (low coverage, parse error in any
    # section, etc.), narrative_sections_clean ends up empty so the
    # ``narrative_article`` module's predicate fails naturally and the
    # page renders module-only — that graceful fallback is the safety
    # mechanism for low-quality narrative output.
    narrative_sections_clean: list[dict[str, Any]] = []
    narrative_telemetry: dict[str, Any] = {}
    from beever_atlas.wiki.modules.narrative_validator import (
        validate_narrative_sections,
    )

    raw_narrative = parsed.get("narrative_sections")
    if not isinstance(raw_narrative, list):
        raw_narrative = []
    try:
        (
            narrative_sections_clean,
            narrative_telemetry,
        ) = validate_narrative_sections(
            raw_narrative,
            facts=top_facts,
        )
    except Exception as exc:  # noqa: BLE001 — validator must never crash render
        logger.exception("narrative_validator_unhandled_exception exc=%s", exc)
        narrative_sections_clean = []
        narrative_telemetry = {
            "rejected": True,
            "reason": "validator_exception",
            "citation_coverage": 0.0,
            "total_words": 0,
            "sections_dropped": 0,
            "paragraphs_dropped": 0,
        }

    page_slug_for_log = str(render_inputs.get("page_id") or title)
    if narrative_telemetry.get("rejected"):
        logger.info(
            "narrative_article_fallback reason=%s page=%s coverage=%.3f",
            narrative_telemetry.get("reason", "unknown"),
            page_slug_for_log,
            float(narrative_telemetry.get("citation_coverage", 0.0)),
        )
    # Surface the article-level telemetry on the orchestrator's
    # log line so soak dashboards can aggregate without re-reading
    # persisted documents.
    logger.info(
        "narrative_article_metrics page=%s section_count=%d total_words=%d "
        "citation_coverage=%.3f distinct_facts_cited=%d rejected=%s",
        page_slug_for_log,
        int(narrative_telemetry.get("section_count", len(narrative_sections_clean))),
        int(narrative_telemetry.get("total_words", 0)),
        float(narrative_telemetry.get("citation_coverage", 0.0)),
        int(narrative_telemetry.get("distinct_facts_cited", 0)),
        bool(narrative_telemetry.get("rejected", False)),
    )
    # Make the narrative payload available to module data builders
    # (the ``narrative_article`` builder reads from render_inputs).
    render_inputs["narrative_sections"] = narrative_sections_clean
    # Refresh the signal so the planner / validator see the post-
    # validation count when picking the ``narrative_article`` module.
    signals = dict(signals)
    signals["narrative_section_count"] = len(narrative_sections_clean)

    # Stage 3 — validate the plan + parse the TL;DR + overview the
    # planner LLM emitted (needed by Stage 4 for hero_summary).
    plan_dict = parsed.get("plan") or {}
    if not isinstance(plan_dict, dict):
        plan_dict = {}
    plan = _validate_plan(plan_dict, signals)
    # Suppression pass — predicates check data SHAPE; this checks
    # rendered SUBSTANCE so we don't ship a 1-child subpage_cards or
    # an entity_diagram dominated by one (source, target) pair.
    plan = _suppress_thin_modules(
        plan,
        signals,
        render_inputs,
        page_id=str(render_inputs.get("page_id") or "<unknown>"),
    )
    if plan.is_empty():
        logger.info("module_compile_plan_empty_fallback — using key_facts only")
        return _fallback_output(title, render_inputs)
    tldr = str(parsed.get("tldr") or "").strip()
    overview = str(parsed.get("overview") or "").strip()

    # Stage 4 — render each module deterministically + attach data
    # payload for the frontend dispatcher.
    rendered_modules: dict[str, str] = {}
    rendered_count = 0
    for entry in plan.modules:
        mid = entry["id"]
        # Defensive contract check — when the LLM picks a module but
        # the gather step didn't populate the keys the renderer
        # needs, the module renders empty and the marker silently
        # disappears. Surface this as a structured warning.
        expected_keys = RENDER_INPUT_KEYS.get(mid, ())
        missing = [k for k in expected_keys if not render_inputs.get(k)]
        if missing:
            logger.warning(
                "module_render_inputs_missing module=%s missing_keys=%s — "
                "module will render empty; gather step likely needs to "
                "populate these keys",
                mid,
                missing,
            )
        data = _per_module_data(mid, render_inputs)
        rendered = _render_python_module(mid, data)
        # ``key_facts`` is a frontend renderer in v2, but we still
        # render the legacy markdown table for the body marker so
        # page.content stays useful for the markdown-render path
        # (older readers, search index, copy-out). The structured
        # v2 payload is attached via ``_extract_media_for_module``
        # for the React dispatcher.
        if not rendered and mid == "key_facts":
            from beever_atlas.wiki.render import render_key_facts_table

            facts_for_legacy = render_inputs.get("facts") or []
            if isinstance(facts_for_legacy, list):
                rendered = render_key_facts_table(facts_for_legacy)
        if rendered:
            rendered_modules[mid] = rendered
            rendered_count += 1
        # Attach data payload for the frontend dispatcher. Two paths:
        # python-renderer modules persist their rendered markdown so
        # the dispatcher can render via WikiMarkdown; frontend-only
        # modules (media + key_facts v2) get the structured data
        # their React component needs.
        spec = MODULE_CATALOG.get(mid)
        if spec and spec.renderer_kind == "frontend":
            entry["data"] = _extract_media_for_module(
                mid,
                render_inputs,
                plan.media_pins,
                tldr=tldr,
                overview=overview,
                signals=signals,
            )
        else:
            # Markdown is intentionally persisted in BOTH ``page.content``
            # (via marker substitution below) and ``module.data.markdown``
            # here. The duplication exists so the frontend dispatcher
            # path can render module-by-module without re-parsing
            # page.content.
            entry["data"] = {
                "label": spec.label if spec else mid,
                "renderer_kind": spec.renderer_kind if spec else "python",
                "markdown": rendered,
            }

    # Post-render Mermaid suppression — drop modules whose rendered
    # output is a Mermaid block with zero ``-->`` edges. Must run
    # AFTER rendering (the check inspects the rendered markdown) but
    # BEFORE substitution (so the dropped marker is left in the body
    # and the substitution pass treats it as a stripped placeholder).
    plan, rendered_modules = _suppress_empty_mermaid_modules(
        plan,
        rendered_modules,
        page_id=str(render_inputs.get("page_id") or "<unknown>"),
    )
    rendered_count = len(rendered_modules)

    # Stage 5 — substitute markers in the body. Hard-fail wraps in a
    # ``ModuleSubstitutionError`` which we catch and degrade to the
    # markers-only fallback body so the page still renders.
    body = str(parsed.get("body") or "").strip()
    try:
        substituted_body = substitute_module_markers(body, rendered_modules)
        fell_back = False
    except ModuleSubstitutionError as exc:
        logger.warning("module_substitution_failed exc=%s", exc)
        substituted_body = ""
        fell_back = True

    # P3 + P4 — post-substitution body cleanup. Strip noisy / empty
    # ``mermaid`` blocks (entity_diagram-style noise that bypassed the
    # module-level suppression) and frontend-module section headers
    # that the LLM left in the body but whose content is rendered by
    # the React dispatcher (so they appear empty in markdown).
    page_id_for_cleanup = str(render_inputs.get("page_id") or "<unknown>")
    substituted_body = _strip_noisy_body_mermaid(substituted_body, page_id=page_id_for_cleanup)
    substituted_body = _strip_empty_frontend_section_headers(
        substituted_body, page_id=page_id_for_cleanup
    )

    # Stage 6 — assemble content from TL;DR + Overview + substituted body.
    # tldr + overview were parsed in Stage 3 so hero_summary's data
    # payload could read them.
    content = _assemble_content(tldr, overview, substituted_body)
    if not content:
        # Catastrophic fallback — produce something so the page isn't
        # empty.
        content = f"**{title}** — page content temporarily unavailable. Try a regenerate."
        fell_back = True

    return ModularPageOutput(
        content=content,
        summary=summary,
        modules=list(plan.modules),
        media_pins=list(plan.media_pins),
        planner_module_count=len(plan.modules),
        rendered_module_count=rendered_count,
        fell_back=fell_back,
        narrative_sections=list(narrative_sections_clean),
        narrative_telemetry=dict(narrative_telemetry),
    )


# ---------------------------------------------------------------------------
# Folder-archetype orchestrator
# ---------------------------------------------------------------------------
#
# Folder index pages are MODULE-ONLY dashboards. The legacy
# ``FOLDER_INDEX_PROMPT`` produced three dense paragraphs of "Themes &
# threads" prose; this function replaces that with a 5-7 module
# dashboard (hero_summary + subpage_cards + folder_stats +
# top_contributors + cross_cutting_decisions + open_questions +
# provenance_drawer). The body has no marker substitution surface —
# the React dispatcher renders each module from ``page.modules`` data,
# and ``page.content`` carries only the bold TL;DR + summary so the
# legacy markdown-render path still has SOMETHING readable.


def _fallback_folder_output(
    folder_title: str,
    descendants: list[dict],
    signals: dict[str, Any],
) -> ModularPageOutput:
    """Deterministic fallback for folder pages — LLM-free dashboard.

    When the LLM crashes / parse fails / plan validates to empty, we
    derive a useful dashboard directly from the descendant aggregate
    instead of emitting a single one-liner. The previous fallback
    shipped only a hero TL;DR + (sometimes) folder_stats, which left
    the rendered page nearly empty whenever the LLM path failed.

    Module sequence (when data is available):
      hero_summary → subpage_cards → folder_stats → top_contributors
      → cross_cutting_decisions → quote_highlights

    Each module is skipped when its data builder yields no items so
    we never ship an empty header. When ``descendants`` is empty we
    fall back to the prior one-liner title-only behaviour so callers
    don't crash on degenerate input. ``fell_back=True`` is preserved
    on the output so telemetry continues to count this as a fallback.
    """
    from beever_atlas.wiki.modules.cross_cutting_decisions import (
        build_cross_cutting_decisions_data,
    )
    from beever_atlas.wiki.modules.folder_stats import build_folder_stats_data
    from beever_atlas.wiki.modules.hero_summary import build_hero_summary_data
    from beever_atlas.wiki.modules.subpage_cards import render as render_subpages
    from beever_atlas.wiki.modules.top_contributors import (
        build_top_contributors_data,
    )

    fallback_tldr = f"**{folder_title} — folder containing {len(descendants)} pages.**"
    fallback_summary = (
        f"Wayfinding index for the {len(descendants)} descendant pages under {folder_title}."
    )

    # Empty-descendants safe path — emit the prior one-liner so the
    # page renders without crashing on degenerate input.
    if not descendants:
        modules: list[dict[str, Any]] = [
            {
                "id": "hero_summary",
                "anchor": "summary",
                "data": build_hero_summary_data(
                    tldr=fallback_tldr,
                    overview=fallback_summary,
                    signals=signals,
                    facts=[],
                ),
            },
        ]
        return ModularPageOutput(
            content=f"{fallback_tldr}\n\n{fallback_summary}",
            summary=fallback_summary,
            modules=modules,
            planner_module_count=len(modules),
            rendered_module_count=len(modules),
            fell_back=True,
        )

    # Aggregate descendant facts so the hero's highlight chips reflect
    # the union (matches the LLM-success path's hero data).
    aggregated_facts: list[dict] = []
    for d in descendants:
        if not isinstance(d, dict):
            continue
        d_facts = d.get("facts") or []
        if isinstance(d_facts, list):
            aggregated_facts.extend(f for f in d_facts if isinstance(f, dict))

    # Build subpage_cards markdown from the descendants directly so
    # the markdown render path shows pages even without a separate
    # ``children`` payload (parity with the success path's cards).
    children_payload = [
        {
            "title": str(d.get("title") or ""),
            "slug": str(d.get("slug") or ""),
            "summary": str(d.get("summary") or "")[:160],
        }
        for d in descendants
        if isinstance(d, dict)
    ]

    modules = [
        {
            "id": "hero_summary",
            "anchor": "summary",
            "data": build_hero_summary_data(
                tldr=fallback_tldr,
                overview=fallback_summary,
                signals=signals,
                facts=aggregated_facts,
            ),
        },
        {
            "id": "subpage_cards",
            "anchor": "subpages",
            "data": {
                "label": "Pages in this section",
                "renderer_kind": "python",
                # Persist BOTH the raw children list and the prerendered
                # markdown blob. ``subpage_cards.render`` reads
                # ``data.children`` to render — without it any
                # downstream consumer that re-runs the renderer (e.g.
                # the markdown serialiser at modules_markdown.py)
                # would emit an empty block.
                "children": children_payload,
                "markdown": render_subpages({"children": children_payload}),
            },
        },
    ]

    if int(signals.get("child_count") or 0) >= 2 or len(descendants) >= 2:
        modules.append(
            {
                "id": "folder_stats",
                "anchor": "folder-stats",
                "data": build_folder_stats_data(descendants),
            }
        )

    contributors_data = build_top_contributors_data(descendants)
    if contributors_data.get("items"):
        modules.append(
            {
                "id": "top_contributors",
                "anchor": "top-contributors",
                "data": contributors_data,
            }
        )

    decisions_data = build_cross_cutting_decisions_data(descendants)
    if decisions_data.get("items"):
        modules.append(
            {
                "id": "cross_cutting_decisions",
                "anchor": "cross-cutting-decisions",
                "data": decisions_data,
            }
        )

    quotes_payload = _build_quote_highlights_from_descendants(descendants)
    if quotes_payload.get("quotes"):
        modules.append(
            {
                "id": "quote_highlights",
                "anchor": "quote-highlights",
                "data": quotes_payload,
            }
        )

    return ModularPageOutput(
        content=f"{fallback_tldr}\n\n{fallback_summary}",
        summary=fallback_summary,
        modules=modules,
        planner_module_count=len(modules),
        rendered_module_count=len(modules),
        fell_back=True,
    )


_QUOTE_IMPORTANCE_SCORES: dict[str, int] = {
    "critical": 9,
    "high": 7,
    "medium": 5,
    "low": 3,
}


def _quote_importance_score(value: Any) -> float:
    """Bucket-or-numeric importance score for fact ranking. Mirrors
    the simple importance buckets used elsewhere in the module
    builders (critical=9, high=7, medium=5, low=3). Numeric inputs
    pass through; non-numeric / unknown strings fall back to 0."""
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if isinstance(value, str):
        return float(_QUOTE_IMPORTANCE_SCORES.get(value.strip().lower(), 0))
    return 0.0


def _build_quote_highlights_from_descendants(
    descendants: list[dict],
    cap: int = 5,
) -> dict[str, Any]:
    """Build a ``quote_highlights`` payload from descendant facts.

    Walks each descendant's ``facts`` list, filters to facts whose
    ``fact_type`` normalises to ``"quote"``, sorts by importance
    score DESC (with quality_score / score fallback for parity with
    the orchestrator's ``_fact_score`` helper), and caps at ``cap``.
    Pure builder — no LLM. Returns the payload shape the existing
    ``quote_highlights.render`` consumes."""
    quotes: list[dict[str, Any]] = []
    for d in descendants or []:
        if not isinstance(d, dict):
            continue
        for f in d.get("facts") or []:
            if not isinstance(f, dict):
                continue
            if str(f.get("fact_type") or "").strip().lower() != "quote":
                continue
            text = str(f.get("memory_text") or f.get("text") or "").strip()
            if not text:
                continue
            quotes.append(f)

    def _score(f: dict) -> float:
        for k in ("importance", "quality_score", "score"):
            v = f.get(k)
            if v is None:
                continue
            s = _quote_importance_score(v) if k == "importance" else 0.0
            if k != "importance" and isinstance(v, (int, float)):
                try:
                    s = float(v)
                except (TypeError, ValueError):
                    s = 0.0
            if s:
                return s
        return 0.0

    quotes.sort(key=_score, reverse=True)
    quotes = quotes[: max(int(cap), 0)]

    formatted = [
        {
            "text": str(f.get("memory_text") or f.get("text") or "").strip(),
            "author": str(f.get("author_name") or f.get("author") or "").strip(),
            "date": str(f.get("message_ts") or f.get("date") or "")[:10],
            "citations": str(f.get("fact_id") or ""),
        }
        for f in quotes
    ]
    return {
        "label": "Voices",
        "renderer_kind": "python",
        "quotes": formatted,
    }


def _folder_catalog_view() -> list[dict[str, Any]]:
    """Build the catalog view for the folder prompt — only the modules
    that can actually fire on a folder page (saves tokens + steers the
    LLM away from picking topic-only modules whose predicates would
    fail the validator)."""
    folder_module_ids = {
        "hero_summary",
        "subpage_cards",
        "folder_stats",
        "top_contributors",
        "cross_cutting_decisions",
        "open_questions",
        "provenance_drawer",
    }
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "description": spec.description,
            "rule": _HUMAN_RULES.get(spec.id, "Pick at planner's discretion."),
        }
        for spec in MODULE_CATALOG.values()
        if spec.id in folder_module_ids
    ]


async def compile_folder_page_modular(
    *,
    folder_title: str,
    folder_slug: str,
    descendants: list[dict],
    children: list[dict],
    llm: LLMCallable,
    compiled_topic_titles: list[str] | None = None,
) -> ModularPageOutput:
    """Single-call folder index compilation.

    Builds one prompt containing the folder-module catalog + signals +
    folder data, invokes the LLM once, parses the unified response,
    validates the plan, and renders each module's data payload.
    Returns a renderable ``ModularPageOutput`` even on failure
    (fall-back is hero_summary + subpage_cards + folder_stats).

    ``descendants`` is the full list of descendant pages, each shaped
    as ``{title, slug, facts: [...]}``. ``children`` is the
    direct-child subset (used by ``subpage_cards``).
    """
    from beever_atlas.wiki.modules.cross_cutting_decisions import (
        build_cross_cutting_decisions_data,
    )
    from beever_atlas.wiki.modules.folder_stats import build_folder_stats_data
    from beever_atlas.wiki.modules.hero_summary import build_hero_summary_data
    from beever_atlas.wiki.modules.planner import compute_signals
    from beever_atlas.wiki.modules.provenance_drawer import (
        build_provenance_drawer_data,
    )
    from beever_atlas.wiki.modules.top_contributors import (
        build_top_contributors_data,
    )
    from beever_atlas.wiki.modules.narrative_validator import (
        validate_narrative_sections,
    )
    from beever_atlas.wiki.prompts import (
        build_module_compile_folder_prompt,
        get_archetype_hint_block,
    )

    # ── Stage 0 — compute signals from the descendant aggregate.
    # The cluster shape feeds the topic-archetype branches with empty
    # values so the topic predicates fail naturally; we override
    # archetype + child_count after to guarantee the folder predicates
    # see the correct values regardless of the topic-side derivation.
    cluster_for_signals = {
        "title": folder_title,
        "member_facts": [],
        "child_count": len(children),
    }
    # Aggregate descendant facts into a flat list so the topic-side
    # signal computations (open_question_count, etc.) see the union of
    # descendants — needed for ``open_questions`` to fire on folders.
    aggregated_facts: list[dict] = []
    aggregated_open_questions: list[dict] = []
    for d in descendants:
        if not isinstance(d, dict):
            continue
        d_facts = d.get("facts") or []
        if isinstance(d_facts, list):
            aggregated_facts.extend(f for f in d_facts if isinstance(f, dict))
        d_oq = d.get("open_questions") or []
        if isinstance(d_oq, list):
            for q in d_oq:
                if isinstance(q, dict):
                    aggregated_open_questions.append(q)
                elif isinstance(q, str) and q.strip():
                    aggregated_open_questions.append({"question": q.strip(), "raised": ""})
    cluster_for_signals["member_facts"] = aggregated_facts

    signals = compute_signals(
        cluster=cluster_for_signals,
        open_questions=aggregated_open_questions,
        descendants=descendants,
    )
    # Force archetype to ``folder`` — the descendant aggregates are
    # what the folder predicates check, and we don't want the
    # topic-archetype derivation (which prioritises ``decision``) to
    # fire on a folder index page.
    signals["archetype"] = "folder"

    # ── Stage 1 — build pre-aggregated payloads for hero + narrative
    # context.
    contributors_data = build_top_contributors_data(descendants)
    top_contributors_for_prompt = contributors_data.get("items", [])
    decisions_data = build_cross_cutting_decisions_data(descendants)
    top_decisions_for_prompt = decisions_data.get("items", [])

    # Top facts surface = highest-importance facts across descendants,
    # capped at 12 to keep prompt tokens manageable. ``importance`` /
    # ``quality_score`` order with a fallback to position.
    def _fact_score(f: dict) -> float:
        for k in ("importance", "quality_score", "score"):
            v = f.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0

    top_facts_for_prompt = sorted(aggregated_facts, key=_fact_score, reverse=True)[:12]

    # ── Stage 2 — single LLM call.
    catalog_view = _folder_catalog_view()
    archetype_hint = get_archetype_hint_block("folder")
    prompt = build_module_compile_folder_prompt(
        signals=signals,
        module_catalog=catalog_view,
        folder_title=folder_title,
        children=children,
        top_contributors=top_contributors_for_prompt,
        top_decisions=top_decisions_for_prompt,
        top_facts=top_facts_for_prompt,
        open_questions=aggregated_open_questions,
        archetype_hint_block=archetype_hint,
        compiled_topic_titles=compiled_topic_titles,
    )

    try:
        result = llm(prompt)
        raw = await result if inspect.isawaitable(result) else result  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001
        logger.exception("module_compile_folder_llm_failed exc=%s", exc)
        return _fallback_folder_output(folder_title, descendants, signals)

    # ── Stage 3 — parse JSON.
    try:
        parsed = _parse_compile_json(str(raw))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "module_compile_folder_parse_failed exc=%s raw_len=%d",
            exc,
            len(str(raw)),
        )
        return _fallback_folder_output(folder_title, descendants, signals)

    # ── Stage 3.5 — extract + validate narrative_sections. Folder
    # narrative articles synthesise across descendants. Validator gates
    # citation discipline + word caps; rejected output → empty list,
    # narrative_article module's predicate fails, page renders module-
    # only (graceful fallback).
    narrative_sections_clean: list[dict[str, Any]] = []
    narrative_telemetry: dict[str, Any] = {}
    raw_narrative = parsed.get("narrative_sections")
    if not isinstance(raw_narrative, list):
        raw_narrative = []
    try:
        (
            narrative_sections_clean,
            narrative_telemetry,
        ) = validate_narrative_sections(
            raw_narrative,
            facts=top_facts_for_prompt,
        )
    except Exception as exc:  # noqa: BLE001 — validator must never crash render
        logger.exception("narrative_validator_unhandled_exception_folder exc=%s", exc)
        narrative_sections_clean = []
        narrative_telemetry = {
            "rejected": True,
            "reason": "validator_exception",
            "citation_coverage": 0.0,
            "total_words": 0,
            "sections_dropped": 0,
            "paragraphs_dropped": 0,
        }
    if narrative_telemetry.get("rejected"):
        logger.info(
            "narrative_article_fallback reason=%s page=folder-%s coverage=%.3f",
            narrative_telemetry.get("reason", "unknown"),
            folder_slug,
            float(narrative_telemetry.get("citation_coverage", 0.0)),
        )
    logger.info(
        "narrative_article_metrics page=folder-%s section_count=%d total_words=%d "
        "citation_coverage=%.3f distinct_facts_cited=%d rejected=%s",
        folder_slug,
        int(narrative_telemetry.get("section_count", len(narrative_sections_clean))),
        int(narrative_telemetry.get("total_words", 0)),
        float(narrative_telemetry.get("citation_coverage", 0.0)),
        int(narrative_telemetry.get("distinct_facts_cited", 0)),
        bool(narrative_telemetry.get("rejected", False)),
    )

    # ── Stage 4 — validate the plan + parse the hero TL;DR + summary.
    plan_dict = parsed.get("plan") or {}
    if not isinstance(plan_dict, dict):
        plan_dict = {}
    # Make narrative_section_count visible to the planner so the
    # narrative_article module's eligibility predicate sees a positive
    # signal when the LLM returned a validated narrative.
    signals = dict(signals)
    signals["narrative_section_count"] = len(narrative_sections_clean)
    plan = _validate_plan(plan_dict, signals)
    if plan.is_empty():
        logger.info("module_compile_folder_plan_empty_fallback")
        return _fallback_folder_output(folder_title, descendants, signals)

    hero = parsed.get("hero") or {}
    if not isinstance(hero, dict):
        hero = {}
    tldr = str(hero.get("tldr") or "").strip()
    overview = str(hero.get("summary") or hero.get("overview") or "").strip()

    # ── Stage 5 — build per-module data payloads. Folder modules are
    # all frontend renderers (no marker substitution); we attach data
    # to each module entry so the React dispatcher can render directly.
    rendered_count = 0
    for entry in plan.modules:
        mid = entry["id"]
        spec = MODULE_CATALOG.get(mid)
        if spec is None:
            continue
        if mid == "hero_summary":
            entry["data"] = build_hero_summary_data(
                tldr=tldr,
                overview=overview,
                signals=signals,
                facts=aggregated_facts,
            )
        elif mid == "narrative_article":
            from beever_atlas.wiki.modules.narrative_article import (
                build_narrative_article_data,
            )

            entry["data"] = build_narrative_article_data(
                narrative_sections_clean,
                aggregated_facts,
            )
        elif mid == "subpage_cards":
            # subpage_cards uses the python renderer (children TOC
            # markdown). Folder pages render children as cards through
            # FolderPage.tsx already — the markdown is a fallback for
            # markdown-render readers.
            from beever_atlas.wiki.modules.subpage_cards import (
                render as render_subpages,
            )

            children_payload = [
                {
                    "title": c.get("title") or "",
                    "slug": c.get("slug") or "",
                    "summary": (c.get("summary") or "")[:160],
                }
                for c in children
            ]
            md = render_subpages({"children": children_payload})
            entry["data"] = {
                "label": spec.label,
                "renderer_kind": "python",
                "markdown": md,
            }
        elif mid == "folder_stats":
            entry["data"] = build_folder_stats_data(descendants)
        elif mid == "top_contributors":
            entry["data"] = build_top_contributors_data(descendants)
        elif mid == "cross_cutting_decisions":
            entry["data"] = build_cross_cutting_decisions_data(descendants)
        elif mid == "open_questions":
            from beever_atlas.wiki.modules.open_questions import (
                render as render_questions,
            )

            md = render_questions({"questions": aggregated_open_questions})
            entry["data"] = {
                "label": spec.label,
                "renderer_kind": "python",
                "markdown": md,
            }
        elif mid == "provenance_drawer":
            entry["data"] = build_provenance_drawer_data(aggregated_facts)
        else:
            entry["data"] = {
                "label": spec.label,
                "renderer_kind": spec.renderer_kind,
            }
        rendered_count += 1

    # ── Stage 6 — assemble content. Folder pages are module-only;
    # ``content`` carries only TL;DR + summary so the legacy markdown
    # render path still shows SOMETHING. The dashboard lives in
    # ``modules`` for the React dispatcher.
    parts: list[str] = []
    if tldr:
        parts.append(tldr if "**" in tldr else f"**{tldr.strip()}**")
    if overview:
        parts.append(overview.strip())
    content = "\n\n".join(parts) or f"**{folder_title}**"

    return ModularPageOutput(
        content=content,
        summary=overview or f"{folder_title} — folder index.",
        modules=list(plan.modules),
        planner_module_count=len(plan.modules),
        rendered_module_count=rendered_count,
        fell_back=False,
    )
