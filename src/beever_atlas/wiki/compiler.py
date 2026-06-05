"""LLM-based wiki page compiler — converts gathered data into WikiPage objects."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from beever_atlas.agents.prompt_safety import wrap_untrusted
from beever_atlas.llm import get_llm_provider
from beever_atlas.llm.model_resolver import is_ollama_model
from beever_atlas.models.domain import (
    AtomicFact,
    WikiCitation,
    WikiPage,
    WikiPageNode,
    WikiPageRef,
    WikiStructure,
)
from beever_atlas.wiki import render
from beever_atlas.wiki.prompts import (
    ACTIVITY_PROMPT,
    DECISIONS_PROMPT,
    FAQ_PROMPT,
    GLOSSARY_PROMPT,
    OVERVIEW_PROMPT,
    PEOPLE_PROMPT,
    SUBTOPIC_PROMPT,
    SUBTOPIC_PROMPT_V2,
    THIN_TOPIC_PROMPT,
    TOPIC_ANALYSIS_PROMPT,
    TOPIC_PROMPT,
    TOPIC_PROMPT_V2,
)
from beever_atlas.wiki.schemas import CompiledPageContent
from beever_atlas.wiki.validators import (
    banned_phrases,
    combine,
    mermaid_balanced,
    min_length,
    required_headings,
)

# Phase 4: deterministic Key Facts marker.
_KEY_FACTS_MARKER = "<<KEY_FACTS_TABLE>>"
# Minimum member_facts count to take the full topic path; below this the
# thin-topic path runs when wiki_compiler_v2 is ON.
_THIN_TOPIC_THRESHOLD = 5


def _splice_key_facts_table(content: str, key_facts: list[dict]) -> str:
    """Replace the `<<KEY_FACTS_TABLE>>` marker with the deterministic table.

    - If the marker is absent, insert the rendered table after the first
      non-TL;DR `## ` heading.
    - If the rendered table is empty, replace the marker with "" (so the
      section vanishes cleanly).
    """
    table = render.render_key_facts_table(key_facts)
    if _KEY_FACTS_MARKER in content:
        return content.replace(_KEY_FACTS_MARKER, table)
    # Marker missing — insert after first non-TL;DR `## ` heading.
    if not table:
        return content
    lines = content.split("\n")
    insert_at: int | None = None
    for idx, line in enumerate(lines):
        if line.startswith("## "):
            # Skip TL;DR heading (case-insensitive match on the remainder).
            heading = line[3:].strip().lower()
            if heading.startswith("tl;dr") or heading.startswith("tldr"):
                continue
            # Insert after this heading plus its following blank line (if any).
            insert_at = idx + 1
            # Skip one blank line to place table content in the section body.
            if insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            break
    if insert_at is None:
        # No suitable heading — append at end.
        return content.rstrip() + "\n\n" + table + "\n"
    new_lines = lines[:insert_at] + [table, ""] + lines[insert_at:]
    return "\n".join(new_lines)


logger = logging.getLogger(__name__)


def _apply_title_fallbacks(clusters: list) -> None:
    """Defend against clusters arriving with empty titles.

    Upstream consolidation is expected to assign a descriptive title to every
    cluster, but if one slips through with ``title == ""`` the LLM will
    faithfully render the empty string (e.g. "**** (1 member) — ..."). Mutate
    each offending cluster in place with a synthesized fallback derived from
    topic_tags, falling back to ``Topic {id[:6]}``.
    """
    for c in clusters:
        if c.title and c.title.strip():
            continue
        fallback = ", ".join(t for t in (c.topic_tags or [])[:3] if t) or f"Topic {c.id[:6]}"
        logger.info(
            "WikiCompiler: cluster %s had empty title, synthesized '%s'",
            c.id,
            fallback,
        )
        c.title = fallback


# Minimum number of member facts in a cluster before sub-page analysis is triggered
TOPIC_SUBPAGE_THRESHOLD = 15

# Minimum number of member facts required to actually dispatch sub-pages.
# _analyze_topic may return needs_subpages=True for mid-size clusters, but
# splitting clusters with fewer than this many facts costs N+1 LLM calls
# for marginal benefit.  Raise to suppress premature splits.
_TOPIC_SUBPAGE_MIN_FACTS = 25

# Minimum number of member facts for a cluster to get its own topic page.
# Kept as the legacy constant for back-compat with callers that don't yet pass
# a tier-resolved override; the tiered policy below is the production path.
TOPIC_MIN_MEMORY_THRESHOLD = 3

# Tiered compile threshold — sparse channels naturally produce low fact-per-topic
# counts; dense channels need the filter to suppress noise. Mirrors the same
# sparse/dense principle the relevance gate already uses (commit b52bff7) so
# small Discord/Slack channels with 1-3 topics still get pages, while 100-topic
# channels keep the noise filter intact.
#
# Format: list of ``(cluster_count_upper_bound, min_facts)`` tuples. Resolution
# picks the first tier whose upper-bound the cluster count fits under; the
# final tuple's bound is ``float("inf")`` to catch everything above the largest
# explicit threshold.
_TOPIC_COMPILE_THRESHOLD_TIERS: list[tuple[float, int]] = [
    (4, 1),  # < 4 clusters: compile any topic with ≥1 fact (very sparse channel)
    (8, 2),  # 4-7 clusters: ≥2 facts
    (16, 3),  # 8-15 clusters: ≥3 facts (current behaviour)
    (float("inf"), 3),  # 16+ clusters: ≥3 facts (unchanged)
]


def _resolve_topic_compile_threshold(cluster_count: int) -> int:
    """Pick the min-facts threshold for ``cluster_count`` total clusters.

    See ``_TOPIC_COMPILE_THRESHOLD_TIERS`` for the table. Falls back to the
    legacy constant when the table is empty / mis-configured so a bad edit
    never silently drops every topic on the floor.
    """
    for upper_bound, min_facts in _TOPIC_COMPILE_THRESHOLD_TIERS:
        if cluster_count < upper_bound:
            return min_facts
    return TOPIC_MIN_MEMORY_THRESHOLD


def _slugify(text: str) -> str:
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug.strip())
    return slug[:80]


_SLACK_TS_RE = re.compile(r"^\d+\.\d+$")


def _build_permalink(fact: AtomicFact) -> str:
    """Build a best-effort permalink to the original message.

    Only Slack permalinks can be built deterministically because Slack
    URLs are channel-id + timestamp and need no per-tenant base URL.
    Mattermost / Teams / Discord need server URL + team-name + post id
    to build a real permalink — none of which live on ``AtomicFact``,
    so we return ``""`` and the citation chip falls back to a non-link
    chip on the frontend.

    Defensive timestamp check: ``fact.platform`` historically defaults
    to ``"slack"`` in several adapter paths even when the data came
    from Mattermost (issue surfaced when self-hosted Mattermost
    citations linked out to ``app.slack.com`` and broke). We guard by
    requiring a Slack-shaped ``message_ts`` (``\\d+\\.\\d+`` —
    unix-seconds with a fractional part) before emitting a Slack URL.
    Mattermost timestamps are ISO-8601 (``2026-04-15T20:27:39.576+00:00``)
    and don't match, so the broken cross-platform URL no longer escapes.
    """
    if not fact.source_message_id and not fact.message_ts:
        return ""
    if fact.platform == "slack" and fact.channel_id and fact.message_ts:
        if _SLACK_TS_RE.match(fact.message_ts):
            ts = fact.message_ts.replace(".", "p")
            return f"https://app.slack.com/archives/{fact.channel_id}/{ts}"
    return ""


def _build_citations(facts: list[AtomicFact]) -> list[WikiCitation]:
    citations = []
    for i, fact in enumerate(facts, 1):
        media_type = fact.source_media_type or None
        media_name = fact.source_media_names[0] if fact.source_media_names else None
        citations.append(
            WikiCitation(
                id=f"[{i}]",
                fact_id=fact.id or "",
                author=fact.author_name,
                timestamp=fact.message_ts,
                text_excerpt=fact.memory_text[:100],
                permalink=_build_permalink(fact),
                media_type=media_type if media_type else None,
                media_name=media_name,
            )
        )
    return citations


def _facts_fallback_content(facts: list[AtomicFact]) -> str:
    """Generate minimal fact-based content when LLM compilation fails."""
    lines = ["_Content generated from source facts — regenerate for full analysis._\n"]
    for f in facts[:5]:
        author = f.author_name or "Unknown"
        text = (f.memory_text or "").strip()
        if text:
            lines.append(f"- **{author}**: {text}")
    return "\n".join(lines) + "\n"


def _compute_size_tier(fact_count: int) -> str:
    """Derive the adaptive-template size tier from a cluster's fact count.

    - "small"  for fewer than 5 facts (render: TL;DR + Key Facts + Sources).
    - "medium" for 5–12 facts (adds Concept Diagram if ≥7 entities + Open Qs).
    - "large"  for more than 12 facts (adds Decisions/Contributors/Tools).
    """
    if fact_count < 5:
        return "small"
    if fact_count <= 12:
        return "medium"
    return "large"


def _normalize_url(url: str) -> str:
    """Normalize a URL for dedup comparison.

    Steps: lowercase scheme+host, canonicalize twitter.com↔x.com, strip query
    string, fragment, and trailing slash. Preserves path case since some
    providers treat the path as case-sensitive.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        # Canonicalize twitter.com → x.com (subdomains too).
        if host == "twitter.com" or host.endswith(".twitter.com"):
            host = "x.com" if host == "twitter.com" else host.replace("twitter.com", "x.com")
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path.rstrip("/") if parsed.path else ""
        # Drop query and fragment (tracking params, shares).
        return urlunparse((scheme, host, path, "", "", ""))
    except Exception as exc:
        logger.debug("_normalize_url failed url=%r: %s", url, exc, exc_info=False)
        return url


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".heic")
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v")
_PDF_EXTS = (".pdf",)
_DOC_EXTS = (".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".md", ".txt", ".rtf")
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "loom.com")


def _derive_media_kind(url: str, name: str, fallback: str) -> str:
    """Derive a stable media kind (``image``/``video``/``pdf``/``document``/``file``/``link``)
    from URL and filename. Falls back to ``fallback`` when the upstream
    persister did not record a specific MIME type.

    Chat-platform attachments (Mattermost / Slack file URLs) often arrive
    with no extension on the URL — `/api/v4/files/<id>` — so we prefer
    the ``name`` field which carries the original filename. Without a
    correct kind, downstream renderers can't decide between an
    ``<img>``, ``<video>``, or PDF preview card.
    """
    fb = (fallback or "").lower().strip()
    # Specific media kinds — trust upstream. ``"link"`` is intentionally
    # NOT in this set: a YouTube URL the persister tagged as "link"
    # should still be promoted to "video" so the VideoEmbedModule
    # picks it up. The generic ``"link"`` fallback is preserved at the
    # bottom of this function for URLs with no media signal.
    if fb in {"image", "video", "pdf", "document", "doc"}:
        return "document" if fb == "doc" else fb

    n = (name or "").lower()
    u = (url or "").lower()

    def _matches(exts: tuple[str, ...]) -> bool:
        # Check the filename suffix (most reliable signal — chat
        # platforms strip extensions from the URL but keep the name)
        # AND the URL path suffix, AND the URL with a `?query` after
        # the extension. Three checks cover the realistic shapes:
        #   logo.png                                     (name)
        #   https://cdn.example/logo.png                 (url path)
        #   https://cdn.example/logo.png?token=abc       (url + query)
        return any(n.endswith(ext) or u.endswith(ext) or f"{ext}?" in u for ext in exts)

    if _matches(_IMAGE_EXTS):
        return "image"
    if _matches(_VIDEO_EXTS):
        return "video"
    if _matches(_PDF_EXTS):
        return "pdf"
    if _matches(_DOC_EXTS):
        return "document"
    if any(host in u for host in _VIDEO_HOSTS):
        return "video"
    # Preserve generic "link" tags from the persister when nothing
    # more specific matched — distinguishes a shared URL from an
    # uploaded file with an unknown extension.
    if fb == "link":
        return "link"
    return "file"


def _build_media_data(facts: list[AtomicFact]) -> list[dict]:
    """Extract media references from facts for the LLM prompt."""

    def _truncate_context(text: str, limit: int = 180) -> str:
        clean = " ".join((text or "").split())
        if len(clean) <= limit:
            return clean
        cut = clean[:limit]
        last_space = cut.rfind(" ")
        if last_space > 40:
            cut = cut[:last_space]
        return cut.rstrip() + "..."

    media: list[dict] = []
    # Track normalized URLs globally across all facts' media+link lists so the
    # same tweet shared in two messages (e.g. x.com/foo and twitter.com/foo/)
    # collapses to a single entry.
    seen_urls: set[str] = set()
    for fact in facts:
        for i, url in enumerate(fact.source_media_urls):
            key = _normalize_url(url)
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            name = (
                fact.source_media_names[i]
                if i < len(fact.source_media_names)
                else url.split("/")[-1]
            )
            kind = _derive_media_kind(url, name, fact.source_media_type or "")
            media.append(
                {
                    "url": url,
                    "type": kind,
                    # ``kind`` is the canonical field the modules orchestrator
                    # reads (planner.py:148 falls back to ``type`` for
                    # legacy callers — keep both in sync).
                    "kind": kind,
                    "name": name,
                    "author": fact.author_name,
                    "context": _truncate_context(fact.memory_text),
                }
            )
        for j, url in enumerate(fact.source_link_urls):
            key = _normalize_url(url)
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            title = fact.source_link_titles[j] if j < len(fact.source_link_titles) else url
            # Promote video-hosted links (YouTube, Vimeo, Loom) so the
            # video module / inline embed picks them up instead of
            # rendering a generic link card.
            link_kind = "video" if any(host in url.lower() for host in _VIDEO_HOSTS) else "link"
            media.append(
                {
                    "url": url,
                    "type": link_kind,
                    "kind": link_kind,
                    "name": title,
                    "author": fact.author_name,
                    "context": _truncate_context(fact.memory_text),
                }
            )
    return media


def _assemble_resources_markdown(media_data: list[dict]) -> str:
    """Build the Resources & Media wiki page markdown deterministically.

    Produces the same section structure that RESOURCES_PROMPT asked the LLM to
    emit, but without an LLM round-trip, avoiding token-limit truncation on
    large channels.

    Sections emitted (each skipped if no data):
      ## Media distribution  — donut chart JSON block
      ## Resources table     — GFM table, max 40 rows, round-robin by type
      ## Overview            — deterministic 1-2 sentence summary
      ## Images              — top 10 image items
      ## Documents           — top 10 document/file/pdf items
      ## Links               — top 20 link items
      ## Videos              — up to 10 video items
    """
    if not media_data:
        return ""

    from collections import Counter

    def _esc(text: str) -> str:
        """Escape pipe characters for GFM table cells and strip newlines."""
        return " ".join(str(text).splitlines()).replace("|", "\\|")

    def _ctx(text: str, limit: int = 120) -> str:
        """Truncate context, sentence-case the result."""
        clean = " ".join(str(text or "").split())[:limit]
        return clean[:1].upper() + clean[1:] if clean else ""

    # ── Type counts ──────────────────────────────────────────────────────
    type_counts: Counter[str] = Counter(item["type"] for item in media_data)

    # ── Section: Media distribution ──────────────────────────────────────
    TYPE_LABELS = {
        "image": "Images",
        "document": "Documents",
        "file": "Files",
        "pdf": "PDFs",
        "link": "Links",
        "video": "Videos",
    }
    chart_data = [
        {"name": TYPE_LABELS.get(t, t.title()), "value": count}
        for t, count in sorted(type_counts.items())
        if count > 0
    ]
    chart_block = (
        "```chart\n"
        + json.dumps(
            {"type": "donut", "title": "Resources by Type", "data": chart_data},
            separators=(",", ":"),
        )
        + "\n```"
    )

    # ── Section: Resources table (round-robin, max 40) ───────────────────
    # Bucket by type and sort each bucket by fact_index (stable ordering).
    buckets: dict[str, list[dict]] = {}
    for item in media_data:
        buckets.setdefault(item["type"], []).append(item)
    # Within each bucket keep insertion order (already stable from _build_media_data).
    type_order = ["image", "document", "file", "pdf", "link", "video"]
    # Include any types not in type_order at the end.
    extra_types = [t for t in buckets if t not in type_order]
    ordered_types = [t for t in type_order if t in buckets] + extra_types

    # Round-robin interleave.
    table_rows: list[dict] = []
    iters = {t: iter(buckets[t]) for t in ordered_types}
    active = list(ordered_types)
    while active and len(table_rows) < 40:
        next_active = []
        for t in active:
            if len(table_rows) >= 40:
                break
            try:
                table_rows.append(next(iters[t]))
                next_active.append(t)
            except StopIteration:
                pass
        active = next_active

    table_lines = [
        "| Name | Type | Shared By | Context | Link |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in table_rows:
        name = _esc(row.get("name", ""))
        rtype = _esc(TYPE_LABELS.get(row.get("type", ""), row.get("type", "").title()))
        author = _esc(row.get("author", ""))
        ctx = _esc(_ctx(row.get("context", "")))
        url = row.get("url", "")
        link_cell = f"[Open]({url})" if url else ""
        table_lines.append(f"| {name} | {rtype} | {author} | {ctx} | {link_cell} |")

    # ── Section: Overview ────────────────────────────────────────────────
    unique_types = sorted(type_counts.keys())
    author_counts: Counter[str] = Counter(
        item.get("author", "") for item in media_data if item.get("author")
    )
    top_author = author_counts.most_common(1)[0][0] if author_counts else None

    type_list = ", ".join(TYPE_LABELS.get(t, t.title()) for t in unique_types)
    overview_parts = [
        f"This channel has shared {len(media_data)} resource(s) across "
        f"{len(unique_types)} type(s): {type_list}."
    ]
    if top_author:
        overview_parts.append(f"Top contributor: {top_author}.")
    overview_text = " ".join(overview_parts)

    # ── Section: Images ──────────────────────────────────────────────────
    images = [m for m in media_data if m.get("type") == "image"][:10]

    # ── Section: Documents ───────────────────────────────────────────────
    docs = [m for m in media_data if m.get("type") in ("document", "file", "pdf")][:10]

    # ── Section: Links ───────────────────────────────────────────────────
    links = [m for m in media_data if m.get("type") == "link"][:20]

    # ── Section: Videos ──────────────────────────────────────────────────
    videos = [m for m in media_data if m.get("type") == "video"][:10]

    # ── Assemble ─────────────────────────────────────────────────────────
    sections: list[str] = []

    sections.append("## Media distribution\n\n" + chart_block)

    if table_rows:
        sections.append("## Resources table\n\n" + "\n".join(table_lines))

    sections.append("## Overview\n\n" + overview_text)

    if images:
        img_lines = ["## Images"]
        for item in images:
            desc = _ctx(item.get("context", ""), 120) or item.get("name", "")
            alt = item.get("name", "image")
            url = item.get("url", "")
            img_lines.append(f"\n**{desc}**\n![{alt}]({url})")
        sections.append("\n".join(img_lines))

    if docs:
        doc_lines = ["## Documents"]
        for item in docs:
            name = item.get("name", "")
            ctx = _ctx(item.get("context", ""), 120)
            url = item.get("url", "")
            # Leading 📄 + the word "PDF" in the link text ensure the
            # frontend `detectMediaType` returns ``"pdf"`` even when the
            # URL is opaque (Mattermost ``/api/v4/files/<id>`` URLs have
            # no extension). The marker also triggers the expandable
            # WikiPdfLink card in WikiMarkdown.
            link_text = f"📄 PDF — {name}" if name else "📄 PDF document"
            doc_lines.append(f"\n**{name}** — {ctx} [{link_text}]({url})")
        sections.append("\n".join(doc_lines))

    if links:
        link_lines = ["## Links"]
        for item in links:
            name = item.get("name", "")
            ctx = _ctx(item.get("context", ""), 120)
            url = item.get("url", "")
            link_lines.append(f"\n**{name}** — {ctx} [Read article]({url})")
        sections.append("\n".join(link_lines))

    if videos:
        vid_lines = ["## Videos"]
        for item in videos:
            desc = _ctx(item.get("context", ""), 120) or item.get("name", "")
            url = item.get("url", "")
            # Leading 🎥 + the word "video" in the link text ensure the
            # frontend `detectMediaType` returns ``"video"`` even when
            # the URL is opaque. Without this, the renderer falls back
            # to a plain text link instead of an embedded ``<video>``
            # element.
            vid_lines.append(f"\n**{desc}** [🎥 Watch video]({url})")
        sections.append("\n".join(vid_lines))

    return "\n\n".join(sections) + "\n"


def _format_relationship_edges(persons: list[dict]) -> list[dict]:
    """Extract relationship edges from person entities for the People prompt."""
    edges: list[dict] = []
    for person_data in persons:
        entity = person_data.get("entity")
        if not entity:
            continue
        person_name = entity.name if hasattr(entity, "name") else str(entity)
        for edge_type in ["decided", "works_on", "uses"]:
            for target in person_data.get(edge_type, []):
                edges.append(
                    {
                        "source": person_name,
                        "relationship": edge_type.upper().replace("_", " "),
                        "target": target,
                    }
                )
    return edges


# Well-known generic terms to exclude from glossary (OS, common apps, hardware, generic dev tools, common infra)
# Localized titles for fixed wiki pages. Keyed by BCP-47 tag then page id.
# Missing tags fall back to English. Keep ids in sync with the WikiPage(id=...)
# values used throughout _compile_* methods.
WIKI_PAGE_TITLES: dict[str, dict[str, str]] = {
    "en": {
        "overview": "Overview",
        "people": "People & Experts",
        "decisions": "Decisions",
        "faq": "FAQ",
        "glossary": "Glossary",
        "activity": "Recent Activity",
        "resources": "Resources & Media",
        "recent_updates": "Recent Updates",
        "project_status": "Project Status & Progress",
        "core_discussions": "Core Discussions",
    },
    "zh-HK": {
        "overview": "概覽",
        "people": "人物與專家",
        "decisions": "決策",
        "faq": "常見問題",
        "glossary": "詞彙表",
        "activity": "近期活動",
        "resources": "資源與媒體",
        "recent_updates": "最新動態",
        "project_status": "項目狀態與進度",
        "core_discussions": "核心討論",
    },
    "zh-TW": {
        "overview": "概覽",
        "people": "人物與專家",
        "decisions": "決策",
        "faq": "常見問題",
        "glossary": "詞彙表",
        "activity": "近期活動",
        "resources": "資源與媒體",
        "recent_updates": "最新動態",
        "project_status": "專案狀態與進度",
        "core_discussions": "核心討論",
    },
    "zh-CN": {
        "overview": "概览",
        "people": "人物与专家",
        "decisions": "决策",
        "faq": "常见问题",
        "glossary": "词汇表",
        "activity": "近期活动",
        "resources": "资源与媒体",
        "recent_updates": "最新动态",
        "project_status": "项目状态与进度",
        "core_discussions": "核心讨论",
    },
    "ja": {
        "overview": "概要",
        "people": "メンバーと専門家",
        "decisions": "意思決定",
        "faq": "よくある質問",
        "glossary": "用語集",
        "activity": "最近のアクティビティ",
        "resources": "リソースとメディア",
        "recent_updates": "最近の更新",
        "project_status": "プロジェクト状況と進捗",
        "core_discussions": "主要な議論",
    },
    "ko": {
        "overview": "개요",
        "people": "인물 및 전문가",
        "decisions": "의사결정",
        "faq": "자주 묻는 질문",
        "glossary": "용어집",
        "activity": "최근 활동",
        "resources": "리소스 및 미디어",
        "recent_updates": "최근 업데이트",
        "project_status": "프로젝트 상태 및 진행 상황",
        "core_discussions": "핵심 논의",
    },
    "es": {
        "overview": "Resumen",
        "people": "Personas y expertos",
        "decisions": "Decisiones",
        "faq": "Preguntas frecuentes",
        "glossary": "Glosario",
        "activity": "Actividad reciente",
        "resources": "Recursos y medios",
        "recent_updates": "Actualizaciones recientes",
        "project_status": "Estado y progreso del proyecto",
        "core_discussions": "Discusiones clave",
    },
    "fr": {
        "overview": "Vue d'ensemble",
        "people": "Personnes et experts",
        "decisions": "Décisions",
        "faq": "FAQ",
        "glossary": "Glossaire",
        "activity": "Activité récente",
        "resources": "Ressources et médias",
        "recent_updates": "Mises à jour récentes",
        "project_status": "État et progression du projet",
        "core_discussions": "Discussions principales",
    },
    "de": {
        "overview": "Übersicht",
        "people": "Personen & Experten",
        "decisions": "Entscheidungen",
        "faq": "FAQ",
        "glossary": "Glossar",
        "activity": "Letzte Aktivität",
        "resources": "Ressourcen & Medien",
        "recent_updates": "Neueste Aktualisierungen",
        "project_status": "Projektstatus & Fortschritt",
        "core_discussions": "Kerndiskussionen",
    },
}


GENERIC_GLOSSARY_TERMS: set[str] = {
    # Operating systems
    "windows",
    "macos",
    "linux",
    "ubuntu",
    "android",
    "ios",
    # Messaging / social
    "whatsapp",
    "imessage",
    "slack",
    "telegram",
    "discord",
    "x",
    "twitter",
    # Hardware
    "mac mini",
    "mac",
    "iphone",
    "ipad",
    # Generic dev tools
    "vs code",
    "visual studio code",
    "github",
    "git",
    "chrome",
    "firefox",
    # Big tech companies
    "google",
    "microsoft",
    "apple",
    "amazon",
    # Well-known infra / databases (generic, not channel-specific)
    "aws",
    "sql",
    "redis",
    "mongodb",
    "sqlite",
    "digital ocean",
    "digital ocean vps",
    "hetzner",
    # Common concepts that don't need defining
    "copilot",
}


def _normalize_faq_content(content: str) -> str:
    """Rewrite drift-form questions into the canonical ``**Q: ?**`` shape.

    The FAQ_PROMPT contract is explicit ("Each topic group MUST be a ##
    heading. Individual Q&A pairs use bold **Q:** formatting on their
    own line"), but the LLM occasionally drifts to ``### Question?`` h3
    form or ``**Question?**`` bare-bold form. Normalising here
    guarantees:
      * Persisted markdown has a single shape regardless of LLM output.
      * Search and MCP read tools see consistent ``**Q:`` markers.
      * Frontend parser falls into Path A (canonical) instead of the
        drift-detection paths.

    The rules:
      * ``### Question text?`` (h3 ending with ``?``) →
        ``**Q: Question text?**\\n\\nA:``  (the rest of the line block
        becomes the answer).
      * ``**Question text?**`` standalone bold line ending with ``?``
        → ``**Q: Question text?**`` (just adds the ``Q:`` prefix).
      * ``## Topic`` and ``## Topic?`` (statement-form, no ``?`` OR
        question-form on h2) are left alone — those are topic
        dividers, not questions, and are followed by Q&A pairs below.

    Idempotent: re-running on already-canonical content is a no-op.
    """
    if not content:
        return content

    out_lines: list[str] = []
    in_q_block = False
    for line in content.split("\n"):
        # h3-form question: ``### Question text?``
        m_h3q = re.match(r"^###\s+(.+\?)\s*$", line)
        if m_h3q:
            out_lines.append(f"**Q: {m_h3q.group(1).strip()}**")
            out_lines.append("")
            out_lines.append("A:")  # answer paragraph follows on next line
            in_q_block = True
            continue

        # Bare-bold question: ``**Question text?**``
        m_boldq = re.match(r"^\*\*([^*]+\?)\*\*\s*$", line)
        if m_boldq and not m_boldq.group(1).startswith("Q:"):
            out_lines.append(f"**Q: {m_boldq.group(1).strip()}**")
            out_lines.append("")
            out_lines.append("A:")
            in_q_block = True
            continue

        # Heading line — closes any in-progress Q block before the
        # next topic divider.
        if re.match(r"^#{1,4}\s+", line):
            in_q_block = False
            out_lines.append(line)
            continue

        # If we just emitted ``A:`` and the next non-blank line is the
        # answer prose, glue them onto the same paragraph the
        # frontend parser expects ("A: <prose>").
        if in_q_block and line.strip() and out_lines and out_lines[-1] == "A:":
            out_lines[-1] = f"A: {line.strip()}"
            continue

        out_lines.append(line)

    return "\n".join(out_lines)


def _faq_fallback(faq_by_topic: list[dict], clusters: list) -> tuple[str, str]:
    """Render a minimal FAQ page from structured inputs when the LLM fails.

    ``faq_by_topic`` is the same structure passed to FAQ_PROMPT:
    ``[{"topic": str, "questions": [{"question": str, "answer": str}, ...]}]``.
    When empty, fall back to one entry per cluster built from topic tags /
    first key_fact.
    """
    lines: list[str] = ["## Frequently Asked Questions", ""]
    rendered_any = False
    for entry in faq_by_topic:
        topic = entry.get("topic", "")
        questions = entry.get("questions") or []
        if not questions:
            continue
        if topic:
            lines.append(f"### {topic}")
            lines.append("")
        for q in questions:
            if isinstance(q, dict):
                q_text = (q.get("question") or "").strip()
                a_text = (q.get("answer") or "").strip()
            else:
                q_text = str(q).strip()
                a_text = ""
            if not q_text:
                continue
            lines.append(f"#### {q_text}")
            if a_text:
                lines.append("")
                lines.append(a_text)
            lines.append("")
            rendered_any = True

    if not rendered_any:
        # No structured Q&A — fall back to per-cluster topic summary.
        for c in clusters:
            title = getattr(c, "title", "") or ""
            if not title.strip():
                continue
            tags = getattr(c, "topic_tags", None) or []
            key_facts = getattr(c, "key_facts", None) or []
            blurb = ""
            if key_facts:
                first = key_facts[0]
                if isinstance(first, dict):
                    blurb = (first.get("fact") or first.get("text") or "").strip()
                else:
                    blurb = str(first).strip()
            if not blurb and tags:
                blurb = ", ".join(str(t) for t in tags[:3])
            lines.append(f"### {title}")
            lines.append("")
            if blurb:
                lines.append(blurb)
                lines.append("")
            rendered_any = True

    if not rendered_any:
        lines.append("_No FAQ data was available for this channel._")
        lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    summary = "Auto-generated FAQ from channel topics."
    return content, summary


_SUBTOPIC_SECTION_ALIASES: dict[str, re.Pattern] = {
    "key_facts": re.compile(r"^##\s+key\s+facts\b", re.IGNORECASE | re.MULTILINE),
    "overview": re.compile(r"^##\s+(overview|summary|details)\b", re.IGNORECASE | re.MULTILINE),
}


def _render_subtopic_key_facts_block(sub_key_facts: list[dict]) -> str:
    if not sub_key_facts:
        return ""
    table = render.render_key_facts_table(sub_key_facts)
    if not table:
        return ""
    return "## Key Facts\n\n" + table


def _render_subtopic_overview_block(sub_title: str, sub_facts: list, parent_title: str) -> str:
    authors: list[str] = []
    for f in sub_facts or []:
        a = (getattr(f, "author_name", "") or "").strip()
        if a and a not in authors:
            authors.append(a)
    count = len(sub_facts or [])
    if count == 0:
        return ""
    author_bit = ""
    if authors:
        shown = ", ".join(authors[:3])
        if len(authors) > 3:
            shown += f" and {len(authors) - 3} others"
        author_bit = f" Contributions came from {shown}."
    return (
        f"## Overview\n\nThis sub-topic of **{parent_title}** consolidates "
        f"{count} related memories under **{sub_title}**.{author_bit}"
    )


def _splice_subtopic_sections(
    content: str,
    sub_title: str,
    sub_facts: list,
    parent_title: str,
    sub_key_facts: list[dict],
) -> str:
    """Append Key Facts table + Overview when the LLM drops them.

    Sub-topic pages sometimes render as TL;DR + concept diagram only, with
    no Key Facts table and no Overview section. This splice injects both
    deterministically so every sub-page has substantive body content.
    """
    if not content:
        return content
    present = {key: bool(pat.search(content)) for key, pat in _SUBTOPIC_SECTION_ALIASES.items()}
    additions: list[str] = []
    if not present["key_facts"]:
        block = _render_subtopic_key_facts_block(sub_key_facts)
        if block:
            additions.append(block)
    if not present["overview"]:
        block = _render_subtopic_overview_block(sub_title, sub_facts, parent_title)
        if block:
            additions.append(block)
    if not additions:
        return content
    logger.info(
        "WikiCompiler: subtopic splice added %d missing sections for '%s': %s",
        len(additions),
        sub_title,
        [k for k, v in present.items() if not v],
    )
    return content.rstrip() + "\n\n" + "\n\n".join(additions) + "\n"


_GLOSSARY_PLACEHOLDER_RE = re.compile(
    r"\(\s*(implicit|inferred|unknown|n\/a|not\s+specified|tbd)\s*\)",
    re.IGNORECASE,
)

_GLOSSARY_SECTION_ALIASES: dict[str, re.Pattern] = {
    "intro": re.compile(r"^##\s+(introduction|overview)\b", re.IGNORECASE | re.MULTILINE),
    "terms": re.compile(r"^##\s+terms?\b", re.IGNORECASE | re.MULTILINE),
}


def _topic_slug_for_title(title: str) -> str:
    """Canonical topic-page slug for a cluster title.

    Single source of truth shared between (a) compile-time WikiPage slug
    assignment in ``_compile_topic_page`` / ``_compile_thin_topic`` and
    (b) wikilink rewriting in the Glossary post-processor. Keeping both
    sides on ``_slugify`` (with the same kebab-case rules) is what makes
    the Glossary's Related-Topics column actually resolve to a real page
    URL instead of opening the "No page yet" modal.
    """
    return _slugify(title or "")


def _build_compiled_topic_slug_index(
    compiled_topic_titles: list[str] | set[str] | None,
) -> dict[str, str]:
    """Build a {lower-cased title: slug} index for wikilink rewriting.

    Lower-casing the key absorbs the (frequent) LLM habit of dropping a
    leading capital on the second word of a multi-word title — the LLM
    emits ``[[hong kong work-from-home policy]]`` while the cluster title
    is ``Hong Kong Work-from-Home Policy``. Both map to the same slug.
    """
    if not compiled_topic_titles:
        return {}
    index: dict[str, str] = {}
    for title in compiled_topic_titles:
        title = (title or "").strip()
        if not title:
            continue
        slug = _topic_slug_for_title(title)
        if slug:
            index.setdefault(title.lower(), slug)
    return index


# ``[[Page Title]]`` matcher. Mirrors the resolver pattern in
# ``services/wiki_maintainer.py`` so anything the LLM emits as a wikilink
# inside the Glossary content gets the same treatment as wikilinks on
# other compiled pages.
_GLOSSARY_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")


def _rewrite_topic_wikilinks(
    content: str,
    compiled_topic_titles: list[str] | set[str] | None,
) -> str:
    """Convert ``[[Title]]`` references in wiki content to native links.

    For each match:
      * if ``Title`` (case-insensitively) matches a compiled topic, emit
        ``[Title](/wiki/<slug>)`` — a real markdown link the renderer
        passes through without consulting the ``cross_links`` map (which
        the compiler-built cache doesn't populate).
      * otherwise drop the brackets and keep the plain title text so the
        page stops surfacing red broken links to topics that were
        skipped by the relevance/threshold gate.

    Operates on the raw markdown string — both inside table cells and in
    the inline ``Used in`` / ``Topic activity`` lines the LLM emits.
    """
    if not content:
        return content
    index = _build_compiled_topic_slug_index(compiled_topic_titles)

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        if not raw:
            return match.group(0)
        slug = index.get(raw.lower())
        if slug:
            # Keep the original casing the LLM emitted so the rendered
            # anchor text matches the surrounding sentence.
            return f"[{raw}](/wiki/{slug})"
        # Unknown / skipped topic — strip the brackets to plain text.
        return raw

    return _GLOSSARY_WIKILINK_RE.sub(_replace, content)


def _collect_glossary_entries(
    glossary_terms: list,
    clusters: list,
    compiled_topic_titles: list[str] | set[str] | None = None,
) -> list[dict]:
    """Aggregate {term, definition, first_mentioned_by, related_topics} rows.

    When ``compiled_topic_titles`` is provided, the ``related_topics`` list
    on each row is filtered to titles that actually have a compiled page;
    titles that the relevance/threshold gate skipped are dropped so the
    Glossary never emits a wikilink to a non-existent destination. When
    ``None``, the legacy behaviour (every cluster title counts) is kept
    so unit tests and ad-hoc callers don't need to plumb the filter.
    """
    allowed_lower: set[str] | None
    if compiled_topic_titles is None:
        allowed_lower = None
    else:
        allowed_lower = {(t or "").strip().lower() for t in compiled_topic_titles if t}
        allowed_lower.discard("")

    def _filter_titles(titles: list[str]) -> list[str]:
        if allowed_lower is None:
            return list(titles)
        return [t for t in titles if t and t.strip().lower() in allowed_lower]

    rows: dict[str, dict] = {}
    for t in glossary_terms or []:
        if isinstance(t, dict):
            name = (t.get("term") or t.get("name") or "").strip()
            if not name:
                continue
            rows[name] = {
                "term": name,
                "definition": (t.get("definition") or t.get("description") or "").strip(),
                "first_mentioned_by": (t.get("first_mentioned_by") or "").strip(),
                "related_topics": _filter_titles(list(t.get("related_topics") or [])),
            }
        elif t:
            name = str(t).strip()
            if name and name not in rows:
                rows[name] = {
                    "term": name,
                    "definition": "",
                    "first_mentioned_by": "",
                    "related_topics": [],
                }
    # Enrich from cluster key_entities. Only clusters that actually compiled
    # to a page contribute — otherwise the Glossary's Related-Topics column
    # would name skipped clusters (the threshold gate dropped them) and the
    # renderer would surface them as red broken links.
    cluster_title_lower_allowed: set[str] | None = allowed_lower
    for c in clusters or []:
        cluster_title = (getattr(c, "title", "") or "").strip()
        if cluster_title_lower_allowed is not None:
            if cluster_title.lower() not in cluster_title_lower_allowed:
                continue
        for ent in getattr(c, "key_entities", None) or []:
            if not isinstance(ent, dict):
                continue
            name = (ent.get("name") or "").strip()
            if not name:
                continue
            row = rows.setdefault(
                name,
                {
                    "term": name,
                    "definition": "",
                    "first_mentioned_by": "",
                    "related_topics": [],
                },
            )
            if not row["definition"]:
                row["definition"] = (ent.get("description") or ent.get("role") or "").strip()
            if cluster_title and cluster_title not in row["related_topics"]:
                row["related_topics"].append(cluster_title)
    return sorted(rows.values(), key=lambda r: r["term"].lower())


def _render_glossary_terms_table(
    entries: list[dict],
    compiled_topic_titles: list[str] | set[str] | None = None,
) -> str:
    """Render the deterministic Glossary Terms table.

    When ``compiled_topic_titles`` is provided, the Related-Topics column
    emits a markdown link ``[Title](/wiki/<slug>)`` for each topic that
    actually has a page; titles that aren't on the compiled list render as
    plain text so the column never produces a red broken link. The slug
    derivation matches the topic-page slug assignment in
    ``_compile_topic_page`` / ``_compile_thin_topic`` exactly — both go
    through ``_slugify``.
    """
    if not entries:
        return ""
    slug_index = _build_compiled_topic_slug_index(compiled_topic_titles)
    lines = [
        "## Terms",
        "",
        "| Term | Definition | First Mentioned By | Related Topics |",
        "|---|---|---|---|",
    ]
    for row in entries:
        term = row["term"].replace("|", "\\|")
        definition = (row["definition"] or "Referenced in this channel.").replace("|", "\\|")
        author = (row["first_mentioned_by"] or "—").replace("|", "\\|")
        related_titles = row["related_topics"][:4] if row["related_topics"] else []
        if related_titles:
            rendered: list[str] = []
            for title in related_titles:
                title_str = (title or "").strip()
                if not title_str:
                    continue
                slug = slug_index.get(title_str.lower())
                if slug:
                    safe = title_str.replace("|", "\\|")
                    rendered.append(f"[{safe}](/wiki/{slug})")
                else:
                    rendered.append(title_str.replace("|", "\\|"))
            related = ", ".join(rendered) if rendered else "—"
        else:
            related = "—"
        lines.append(f"| {term} | {definition} | {author} | {related} |")
    return "\n".join(lines)


def _splice_glossary_sections(
    content: str,
    glossary_terms: list,
    clusters: list,
    compiled_topic_titles: list[str] | set[str] | None = None,
) -> str:
    """Append deterministic Introduction + Terms table when the LLM drops them.

    The Glossary prompt sometimes emits only the relationship mermaid diagram
    and nothing else. This helper detects missing Introduction and Terms
    sections via heading-alias regex and appends deterministic replacements.
    ``compiled_topic_titles`` (when provided) is forwarded to the entry
    collector + renderer so the spliced table never references skipped
    topics.
    """
    if not content:
        return content
    present = {key: bool(pat.search(content)) for key, pat in _GLOSSARY_SECTION_ALIASES.items()}
    additions: list[str] = []
    if not present["intro"]:
        additions.append(
            "## Introduction\n\nKey terms, acronyms, and concepts used in this channel."
        )
    if not present["terms"]:
        entries = _collect_glossary_entries(
            glossary_terms, clusters, compiled_topic_titles=compiled_topic_titles
        )
        block = _render_glossary_terms_table(entries, compiled_topic_titles=compiled_topic_titles)
        if block:
            additions.append(block)
    if not additions:
        return content
    logger.info(
        "WikiCompiler: glossary splice added %d missing sections: %s",
        len(additions),
        [k for k, v in present.items() if not v],
    )
    return content.rstrip() + "\n\n" + "\n\n".join(additions) + "\n"


def _scrub_glossary_placeholders(content: str) -> str:
    """Replace `(Implicit)` / `(Inferred)` / `(Unknown)` markers with `—`.

    The Glossary prompt now forbids these, but LLM outputs occasionally slip
    them into the "First Mentioned By" column. Swapping them for an em-dash
    keeps the column readable without revealing LLM guesswork.
    """
    if not content:
        return content
    return _GLOSSARY_PLACEHOLDER_RE.sub("—", content)


def _glossary_fallback(glossary_terms: list, clusters: list) -> tuple[str, str]:
    """Render a minimal Glossary page from term list / clusters when LLM fails."""
    lines: list[str] = ["## Glossary", ""]

    # Build term -> definition map from cluster key_entities where available.
    defs: dict[str, str] = {}
    for c in clusters:
        for ent in getattr(c, "key_entities", None) or []:
            if isinstance(ent, dict):
                name = (ent.get("name") or "").strip()
                desc = (ent.get("description") or ent.get("role") or "").strip()
                if name and desc and name not in defs:
                    defs[name] = desc

    normalized_terms: list[str] = []
    for t in glossary_terms:
        if isinstance(t, dict):
            name = (t.get("term") or t.get("name") or "").strip()
            if name:
                normalized_terms.append(name)
                desc = (t.get("definition") or t.get("description") or "").strip()
                if desc and name not in defs:
                    defs[name] = desc
        elif t:
            normalized_terms.append(str(t).strip())

    rendered_any = False
    for term in sorted(set(normalized_terms), key=str.lower):
        if not term:
            continue
        definition = defs.get(term, "").strip()
        if not definition:
            definition = "Referenced in this channel."
        lines.append(f"**{term}** — {definition}")
        lines.append("")
        rendered_any = True

    if not rendered_any:
        # No term list — fall back to cluster titles + first key fact.
        for c in clusters:
            title = getattr(c, "title", "") or ""
            if not title.strip():
                continue
            key_facts = getattr(c, "key_facts", None) or []
            blurb = ""
            if key_facts:
                first = key_facts[0]
                if isinstance(first, dict):
                    blurb = (first.get("fact") or first.get("text") or "").strip()
                else:
                    blurb = str(first).strip()
            if not blurb:
                blurb = "Discussed in this channel."
            lines.append(f"**{title}** — {blurb}")
            lines.append("")
            rendered_any = True

    if not rendered_any:
        lines.append("_No glossary terms were extracted for this channel._")
        lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    summary = "Auto-generated glossary from channel terms."
    return content, summary


_PLACEHOLDER_LINE = "This section has limited data and could not be summarized."


# Headings the Overview template is supposed to emit. Each entry is
# (canonical_heading, alias_regex) — alias_regex matches H2 headings
# case-insensitively so we don't double-insert when the LLM picks a variant.
_OVERVIEW_SECTION_ALIASES: dict[str, re.Pattern] = {
    "key_highlights": re.compile(r"^##\s+(key\s+)?highlights\b", re.IGNORECASE | re.MULTILINE),
    "topics": re.compile(r"^##\s+topics(\s+at\s+a\s+glance)?\b", re.IGNORECASE | re.MULTILINE),
    "contributors": re.compile(r"^##\s+(key\s+)?contributors\b", re.IGNORECASE | re.MULTILINE),
    "tools": re.compile(
        r"^##\s+tools(\s*&\s*resources|\s+and\s+resources)?\b", re.IGNORECASE | re.MULTILINE
    ),
    "momentum": re.compile(r"^##\s+(recent\s+)?momentum\b", re.IGNORECASE | re.MULTILINE),
}


def _render_overview_key_highlights(
    topic_count: int,
    decisions_count: int,
    people_count: int,
    media_count: int,
    date_range: tuple[str, str] | None = None,
) -> str:
    rows = [
        ("Topics", str(topic_count)),
        ("Decisions Made", str(decisions_count)),
        ("Key Contributors", str(people_count)),
        ("Resources Shared", str(media_count)),
    ]
    if date_range and (date_range[0] or date_range[1]):
        start = (date_range[0] or "").strip()[:10]
        end = (date_range[1] or "").strip()[:10]
        if start and end:
            rows.append(("Active Period", f"{start} – {end}"))
        elif start or end:
            rows.append(("Active Period", start or end))
    lines = ["## Key Highlights", "", "| Metric | Value |", "|---|---|"]
    for label, value in rows:
        lines.append(f"| {label} | {value} |")
    return "\n".join(lines)


def _render_overview_topics(clusters: list, skipped_titles: set[str]) -> str:
    if not clusters:
        return ""
    lines = ["## Topics at a glance", ""]
    for c in clusters:
        title = (getattr(c, "title", "") or "").strip() or f"Topic {getattr(c, 'id', '')[:6]}"
        mc = getattr(c, "member_count", 0)
        blurb = ""
        tags = getattr(c, "topic_tags", None) or []
        if tags:
            blurb = str(tags[0])
        blurb = (blurb or "")[:120]
        suffix = f" — {blurb}" if blurb else ""
        brief = " (brief mention)" if title in skipped_titles else ""
        lines.append(f"- **{title}** ({mc} memories){suffix}{brief}")
    return "\n".join(lines)


def _render_overview_contributors(top_people: list) -> str:
    if not top_people:
        return ""
    lines = ["## Key contributors", ""]
    for person in top_people[:8]:
        if isinstance(person, dict):
            name = (person.get("name") or "").strip()
            role = (person.get("role") or "").strip()
            expertise = person.get("expertise_topics") or []
        else:
            name = str(person).strip()
            role = ""
            expertise = []
        if not name:
            continue
        bits = []
        if role:
            bits.append(role)
        elif expertise:
            bits.append(str(expertise[0]))
        suffix = f" — {' · '.join(bits)}" if bits else ""
        lines.append(f"- **{name}**{suffix}")
    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


_GENERIC_TOOLS = {
    "slack",
    "whatsapp",
    "imessage",
    "x",
    "twitter",
    "macos",
    "linux",
    "windows",
    "vs code",
    "vscode",
    "github",
}


def _render_overview_tools(tech_data: list[dict], project_data: list[dict]) -> str:
    tools: list[str] = []
    seen: set[str] = set()
    for t in tech_data or []:
        name = (t.get("name") or "").strip()
        if not name or name.lower() in _GENERIC_TOOLS:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        tools.append(name)
        if len(tools) >= 10:
            break
    for p in project_data or []:
        if len(tools) >= 10:
            break
        name = (p.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        tools.append(name)
    if not tools:
        return ""
    lines = ["## Tools & resources", ""]
    for name in tools:
        lines.append(f"- {name}")
    return "\n".join(lines)


def _render_overview_momentum(momentum_text: str, recent: dict) -> str:
    text = (momentum_text or "").strip()
    highlights = []
    if isinstance(recent, dict):
        raw_highlights = recent.get("highlights") or []
        for h in raw_highlights[:3]:
            if isinstance(h, str) and h.strip():
                highlights.append(h.strip())
            elif isinstance(h, dict):
                val = (h.get("text") or h.get("title") or "").strip()
                if val:
                    highlights.append(val)
    if not text and not highlights:
        return ""
    lines = ["## Recent momentum", ""]
    if text:
        lines.append(text)
        lines.append("")
    for h in highlights:
        lines.append(f"- {h}")
    return "\n".join(lines).rstrip()


_FOLDER_COMPILE_PARALLELISM: int = 4
"""Max simultaneous in-flight folder-page LLM compiles per
``ready_now`` topology batch in ``compile_folders``.

The folder pipeline has historically been serial — a 4-folder
channel paid 4× the LLM round-trip latency end-to-end, even though
sibling folders within one batch have no inter-dependency.
Parallelising at the gather() level cuts the wall-clock from
``N × 16s`` to roughly ``ceil(N/4) × 16s``.

Cap is 4 (not unbounded) so a pathological wiki shape (e.g. 50
sibling folders) doesn't hammer the LLM provider past its quota
and trip the CircuitBreaker mid-regenerate. Tune in code if Gemini
quota grows; not exposed as an env var because operators have no
reason to lower this (lower = strictly slower)."""

_TOPIC_COMPILE_PARALLELISM: int = 6
"""Max simultaneous in-flight topic-page LLM compiles in ``compile_pages``.

Default 6 matches the Gemini Flash RPM ceiling (~360 RPM = 6 RPS) so a
wide channel (50+ topics) doesn't trip the provider quota mid-regenerate.
Overrideable via ``Settings.wiki_topic_compile_parallelism`` (env var
``WIKI_TOPIC_COMPILE_PARALLELISM``, range 1–16). Set to 16 for ultra-large
channels on paid high-quota tiers."""


def _rollup_folder_child_phantom_facts(modules: list[Any]) -> list[dict[str, Any]]:
    """Synthesize phantom facts from a sub-folder's persisted module data.

    Walks a sub-folder's ``folder_stats`` (already-aggregated counts)
    and ``top_contributors`` (author roster) modules to produce a fact
    list that re-aggregates to the same numbers when fed into a parent
    folder's ``build_folder_stats_data`` / ``build_top_contributors_data``.

    Background: ``compile_folders`` topologically sorts so child
    folders compile first; their parents receive already-compiled
    ``WikiPage`` objects via ``children_pages``. A folder page has
    no direct citations and its ``modules`` list contains
    ``folder_stats``/``top_contributors``/``subpage_cards`` rather
    than ``decision_log``/``quote_highlights``. So the leaf-style
    F2 promotion (which looks for ``decision_log`` etc.) finds
    nothing → the parent's ``descendants_payload[i].facts`` stays
    empty → the parent's ``folder_stats`` aggregates 0/0/0/0
    despite the sub-folder containing hundreds of memories.

    Phantom facts use empty ``memory_text`` so they DO NOT pollute
    quote_highlights or other content-rendering modules — they only
    move the numeric counts. Each contributor's name is assigned to
    at least one phantom fact so the distinct-contributor count
    rolls up correctly.

    Returns an empty list when the sub-folder has no folder_stats
    AND no top_contributors entries — the caller can safely append
    the (empty) list and proceed.
    """
    total_memories = 0
    total_decisions = 0
    total_questions = 0
    contributor_names: list[str] = []
    for mod in modules or []:
        if not isinstance(mod, dict):
            continue
        mod_id = mod.get("id")
        inner = mod.get("data") or {}
        if not isinstance(inner, dict):
            continue
        if mod_id == "folder_stats":
            for stat in inner.get("stats") or []:
                if not isinstance(stat, dict):
                    continue
                label = str(stat.get("label") or "").strip().lower()
                try:
                    val = int(stat.get("value") or 0)
                except (TypeError, ValueError):
                    val = 0
                if label == "memories":
                    total_memories = val
                elif label == "decisions":
                    total_decisions = val
                elif label in ("open questions", "questions"):
                    total_questions = val
        elif mod_id == "top_contributors":
            for item in inner.get("items") or []:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("author") or "").strip()
                    if name:
                        contributor_names.append(name)

    if (
        total_memories == 0
        and total_decisions == 0
        and total_questions == 0
        and not contributor_names
    ):
        return []

    phantom: list[dict[str, Any]] = []

    def _author_for(idx: int) -> str:
        if not contributor_names:
            return ""
        return contributor_names[idx % len(contributor_names)]

    # Decision-typed phantoms come first so author rotation puts
    # contributors on decisions (more likely to surface in
    # cross_cutting_decisions builders).
    for i in range(total_decisions):
        phantom.append(
            {
                "fact_id": "",
                "memory_text": "",
                "author_name": _author_for(i),
                "fact_type": "decision",
                "importance": "high",
                "message_ts": "",
            }
        )
    for i in range(total_questions):
        phantom.append(
            {
                "fact_id": "",
                "memory_text": "",
                "author_name": _author_for(total_decisions + i),
                "fact_type": "question",
                "importance": "medium",
                "message_ts": "",
            }
        )
    # Untyped memories fill the remainder so total = total_memories.
    # ``folder_stats.build_folder_stats_data`` counts ALL facts as
    # memories, then adds typed ones to the matching bucket — so
    # decisions+questions are already counted in total_memories.
    remaining = max(total_memories - total_decisions - total_questions, 0)
    for i in range(remaining):
        phantom.append(
            {
                "fact_id": "",
                "memory_text": "",
                "author_name": _author_for(total_decisions + total_questions + i),
                "fact_type": "",
                "message_ts": "",
            }
        )
    # Ensure every contributor name appears at least once even when
    # the phantom-fact count is smaller than the contributor roster
    # (e.g. a sparse folder with 5 contributors but only 3 memories).
    if contributor_names:
        seen_authors = {f["author_name"] for f in phantom}
        for name in contributor_names:
            if name not in seen_authors:
                phantom.append(
                    {
                        "fact_id": "",
                        "memory_text": "",
                        "author_name": name,
                        "fact_type": "",
                        "message_ts": "",
                    }
                )
                seen_authors.add(name)
    return phantom


def _splice_overview_sections(
    content: str,
    channel_summary: Any,
    clusters: list,
    tech_data: list[dict],
    project_data: list[dict],
    decisions_count: int,
    skipped_topics: list[dict],
) -> str:
    """Append deterministic sections the LLM omitted.

    Overview generation is unstable — the LLM often drops Key Highlights,
    Topics at a glance, Contributors, Tools, or Momentum. We detect missing
    H2 sections by heading alias regex and append deterministic replacements
    in the template order. Existing sections are left untouched.
    """
    if not content:
        return content
    present = {key: bool(pat.search(content)) for key, pat in _OVERVIEW_SECTION_ALIASES.items()}
    skipped_titles = {
        (st.get("title") or "").strip() for st in skipped_topics or [] if isinstance(st, dict)
    }

    additions: list[str] = []
    if not present["key_highlights"]:
        additions.append(
            _render_overview_key_highlights(
                topic_count=len(clusters),
                decisions_count=decisions_count,
                people_count=len(getattr(channel_summary, "top_people", []) or []),
                media_count=getattr(channel_summary, "media_count", 0) or 0,
                date_range=(
                    getattr(channel_summary, "date_range_start", ""),
                    getattr(channel_summary, "date_range_end", ""),
                ),
            )
        )
    if not present["topics"]:
        block = _render_overview_topics(clusters, skipped_titles)
        if block:
            additions.append(block)
    if not present["contributors"]:
        block = _render_overview_contributors(getattr(channel_summary, "top_people", []) or [])
        if block:
            additions.append(block)
    if not present["tools"]:
        block = _render_overview_tools(tech_data, project_data)
        if block:
            additions.append(block)
    if not present["momentum"]:
        block = _render_overview_momentum(
            getattr(channel_summary, "momentum", "") or "",
            getattr(channel_summary, "recent_activity_summary", {}) or {},
        )
        if block:
            additions.append(block)

    if not additions:
        return content
    logger.info(
        "WikiCompiler: overview splice added %d missing sections: %s",
        len(additions),
        [k for k, v in present.items() if not v],
    )
    return content.rstrip() + "\n\n" + "\n\n".join(additions) + "\n"


def _t10n(lang: str, key: str) -> str:
    """Look up a localized title from ``WIKI_PAGE_TITLES`` with English
    fallback. Used by the per-section splicers below so headings render
    in the target language regardless of LLM behaviour."""
    lang_map = WIKI_PAGE_TITLES.get(lang) or WIKI_PAGE_TITLES["en"]
    return lang_map.get(key) or WIKI_PAGE_TITLES["en"].get(key) or key.replace("_", " ").title()


def _lang_has_translations(lang: str) -> bool:
    """Return True when ``lang`` has its own entry in ``WIKI_PAGE_TITLES``.

    The deterministic splicers below skip when the locale is missing
    rather than splicing an English-named heading into a body the
    LLM already translated to the target language. ``WIKI_PAGE_TITLES``
    currently ships ~9 locales while ``supported_languages`` ships
    ~30; for the unsupported ones we trust the LLM-side prose to
    surface the same content rather than risk mixed-language output.
    """
    return lang in WIKI_PAGE_TITLES


def _has_localized_h2(body: str, title: str) -> bool:
    """Return True when ``body`` already contains an H2 heading whose
    text matches ``title`` (case-insensitive, ignoring decoration).

    Idempotency guard for the per-section splicers. The LLM is free
    to emit decorated headings — ``## **Recent Updates**`` (bold),
    ``## 1. Recent Updates`` (numbered), ``## Recent Updates 🚀``
    (emoji), ``## Recent Updates {#anchor}`` (anchor). The strict
    ``^## TITLE$`` form misses all of those and would cause the
    splicer to insert a duplicate section. Loosen detection to
    "title appears as a word inside any H2", ignoring leading
    numbering / bold / italic markers and trailing emoji / anchor.
    """
    if not body or not title:
        return False
    pattern = re.compile(
        # ^##\s+ — H2 marker
        # (\*+|_+|\d+\.\s|\s)* — optional leading bold/italic/numbering
        # <escaped title as a word boundary> — case-insensitive title
        r"^##\s+(?:\*+|_+|\d+\.\s+|\s)*\b" + re.escape(title.strip()) + r"\b",
        re.IGNORECASE | re.MULTILINE,
    )
    return bool(pattern.search(body))


def _splice_recent_updates(
    body: str,
    *,
    recent_activity_summary: dict,
    lang: str,
) -> str:
    """Append a deterministic ``Recent Updates`` H2 to ``body``.

    Pulls from ``recent_activity_summary`` (the dict
    ``ChannelSummary.recent_activity_summary`` carries — keys
    ``facts_added_7d``, ``decisions_added_7d``, ``new_topics``,
    ``updated_topics``, ``highlights``). Caps at ~5 bullets so the
    section stays glanceable. Returns ``body`` unchanged when the
    input is empty OR a section with the localized heading already
    exists (case-insensitive match)."""
    if not isinstance(recent_activity_summary, dict) or not recent_activity_summary:
        return body
    # Skip when the locale lacks a translation entry — splicing an
    # English heading into a translated body produces visibly mixed
    # output. See ``_lang_has_translations`` for rationale.
    if not _lang_has_translations(lang):
        return body
    title = _t10n(lang, "recent_updates")
    if _has_localized_h2(body, title):
        return body

    facts_added = int(recent_activity_summary.get("facts_added_7d") or 0)
    decisions_added = int(recent_activity_summary.get("decisions_added_7d") or 0)
    new_topics = recent_activity_summary.get("new_topics") or []
    updated_topics = recent_activity_summary.get("updated_topics") or []
    highlights = recent_activity_summary.get("highlights") or []

    bullets: list[str] = []
    if facts_added or decisions_added:
        bullets.append(
            f"- {facts_added} new memories added in the last 7 days"
            + (f" ({decisions_added} decisions)" if decisions_added else "")
        )
    if new_topics:
        names = [str(t).strip() for t in new_topics if str(t).strip()]
        if names:
            bullets.append(f"- New topics: {', '.join(names[:5])}")
    if updated_topics:
        names = [str(t).strip() for t in updated_topics if str(t).strip()]
        if names:
            bullets.append(f"- Updated topics: {', '.join(names[:5])}")
    for h in highlights:
        if len(bullets) >= 5:
            break
        if isinstance(h, str) and h.strip():
            bullets.append(f"- {h.strip()}")
        elif isinstance(h, dict):
            text = (h.get("memory_text") or h.get("text") or h.get("title") or "").strip()
            if text:
                author = (h.get("author_name") or h.get("author") or "").strip()
                snippet = text[:160]
                if author:
                    bullets.append(f"- **{author}** — {snippet}")
                else:
                    bullets.append(f"- {snippet}")

    if not bullets:
        return body
    bullets = bullets[:5]
    section = f"## {title}\n\n" + "\n".join(bullets)
    return body.rstrip() + "\n\n" + section + "\n"


def _splice_project_status(
    body: str,
    *,
    momentum: str | dict | None,
    team_dynamics: str | dict | None,
    lang: str,
) -> str:
    """Append a deterministic ``Project Status & Progress`` H2 to ``body``.

    ``momentum`` is the channel-level momentum prose. The domain model
    types it as ``str`` (``models/domain.py``); ``dict`` and ``None``
    are accepted as forward-compat shapes (an upstream enrichment can
    surface a structured momentum object without breaking this call
    site). Same applies to ``team_dynamics``. When both normalise to
    empty / blank, the section is skipped. Idempotent against an
    existing localized H2 with the same title."""
    # Skip when the locale lacks a translation entry — see
    # ``_lang_has_translations`` for rationale.
    if not _lang_has_translations(lang):
        return body
    title = _t10n(lang, "project_status")
    if _has_localized_h2(body, title):
        return body

    # Both inputs are typed ``str`` on the domain model but the
    # function takes ``dict`` per the spec; tolerate both so callers
    # that already have prose can pass it directly.
    def _normalize(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, dict):
            for k in ("text", "summary", "description"):
                t = v.get(k)
                if isinstance(t, str) and t.strip():
                    return t.strip()
        return str(v).strip()

    momentum_text = _normalize(momentum)
    team_text = _normalize(team_dynamics)

    # De-dupe with the legacy ``_splice_overview_sections`` which
    # emits ``## Recent momentum`` from the same ``momentum`` field.
    # If that section is already in the body, drop our momentum bullet
    # so the user doesn't see the same prose twice. ``team_dynamics``
    # has no overlap and stays.
    if momentum_text and _has_localized_h2(body, "Recent momentum"):
        momentum_text = ""

    if not momentum_text and not team_text:
        return body

    bullets: list[str] = []
    if momentum_text:
        bullets.append(f"- **Momentum** — {momentum_text}")
    if team_text:
        bullets.append(f"- **Team dynamics** — {team_text}")

    section = f"## {title}\n\n" + "\n".join(bullets)
    return body.rstrip() + "\n\n" + section + "\n"


def _splice_core_discussions(
    body: str,
    *,
    top_decisions: list,
    cited_facts: list,
    lang: str,
) -> str:
    """Append a deterministic ``Core Discussions`` H2 to ``body``.

    Renders up to 3 top decisions (using the same shape the
    ``top_decisions`` enrichment carries — ``name`` / ``decided_by``
    / ``date``) plus up to 3 high-importance fact quotes (drawn from
    the ``cited_facts_for_prompt`` list which already carries an
    ``index`` field — that maps 1:1 to the inline ``[N]`` citation
    markers the rest of the page uses). Skipped when both inputs
    are empty. Idempotent against an existing localized H2."""
    # Skip when the locale lacks a translation entry — see
    # ``_lang_has_translations`` for rationale.
    if not _lang_has_translations(lang):
        return body
    title = _t10n(lang, "core_discussions")
    if _has_localized_h2(body, title):
        return body

    decisions = [d for d in (top_decisions or []) if isinstance(d, dict)][:3]
    quotes_in: list[dict] = []
    for f in cited_facts or []:
        if not isinstance(f, dict):
            continue
        if (f.get("excerpt") or f.get("memory_text") or "").strip():
            quotes_in.append(f)
    quotes_in = quotes_in[:3]

    if not decisions and not quotes_in:
        return body

    lines: list[str] = [f"## {title}", ""]
    if decisions:
        lines.append("### Top decisions")
        lines.append("")
        for d in decisions:
            name = (d.get("name") or d.get("title") or "").strip()
            if not name:
                continue
            decided_by = (d.get("decided_by") or "").strip()
            date = (d.get("date") or "").strip()[:10]
            bits: list[str] = []
            if decided_by:
                bits.append(decided_by)
            if date:
                bits.append(date)
            suffix = f" — {' · '.join(bits)}" if bits else ""
            lines.append(f"- **{name}**{suffix}")
        lines.append("")
    if quotes_in:
        from beever_atlas.wiki.render import _strip_untrusted_wrapper

        lines.append("### Key voices")
        lines.append("")
        for q in quotes_in:
            # ``cited_facts_for_prompt`` wraps every excerpt with the
            # ``<untrusted>...</untrusted>`` prompt-safety marker for
            # the LLM context — those tags must be stripped before the
            # text reaches the rendered markdown the user sees.
            raw_text = q.get("excerpt") or q.get("memory_text") or ""
            text = _strip_untrusted_wrapper(str(raw_text)).strip()
            if not text:
                continue
            author = (q.get("author") or q.get("author_name") or "").strip()
            idx = q.get("index")
            cite = f" [{int(idx)}]" if isinstance(idx, int) else ""
            attribution = f" — {author}" if author else ""
            lines.append(f'> "{text}"{attribution}{cite}')
            lines.append("")
        # Trim trailing blank line for clean joining.
        while lines and not lines[-1]:
            lines.pop()

    section = "\n".join(lines).rstrip()
    return body.rstrip() + "\n\n" + section + "\n"


def _overview_fallback(channel_summary: Any, clusters: list) -> tuple[str, str]:
    """Render a minimal Overview page from channel summary + clusters."""
    channel_name = getattr(channel_summary, "channel_name", "") or "This channel"
    description = (getattr(channel_summary, "description", "") or "").strip()
    themes = (getattr(channel_summary, "themes", "") or "").strip()
    text = (getattr(channel_summary, "text", "") or "").strip()
    blurb = description or themes or text
    topic_count = len(clusters)
    memory_count = sum(getattr(c, "member_count", 0) for c in clusters)

    lines: list[str] = ["## Overview", ""]
    para_parts = [f"**{channel_name}**"]
    if blurb:
        para_parts.append(blurb)
    para_parts.append(f"{topic_count} topic(s), {memory_count} memories.")
    lines.append(" ".join(para_parts))
    lines.append("")

    if clusters:
        lines.append("## Topics at a glance")
        lines.append("")
        for c in clusters:
            title = (getattr(c, "title", "") or "").strip() or f"Topic {getattr(c, 'id', '')[:6]}"
            mc = getattr(c, "member_count", 0)
            tags = getattr(c, "topic_tags", None) or []
            blurb2 = ""
            if tags:
                blurb2 = str(tags[0])
            else:
                kf = getattr(c, "key_facts", None) or []
                if kf:
                    first = kf[0]
                    if isinstance(first, dict):
                        blurb2 = (
                            first.get("fact") or first.get("memory_text") or first.get("text") or ""
                        ).strip()
                    else:
                        blurb2 = str(first).strip()
            blurb2 = blurb2[:120]
            suffix = f" — {blurb2}" if blurb2 else ""
            lines.append(f"* **{title}** ({mc} memories){suffix}")
        lines.append("")

    top_people = getattr(channel_summary, "top_people", None) or []
    if top_people:
        lines.append("## Key contributors")
        lines.append("")
        for person in top_people[:5]:
            if isinstance(person, dict):
                name = (person.get("name") or "").strip()
            else:
                name = str(person).strip()
            if name:
                lines.append(f"* {name}")
        lines.append("")

    if len(lines) <= 2:
        lines.append(_PLACEHOLDER_LINE)

    content = "\n".join(lines).rstrip() + "\n"
    summary = f"Overview of {channel_name} ({topic_count} topics, {memory_count} memories)."
    return content, summary


def _people_fallback(persons: list, top_people: list) -> tuple[str, str]:
    """Render a minimal Contributors page from persons / top_people data."""
    lines: list[str] = ["## Contributors", ""]
    rendered_any = False

    # Prefer structured `persons` (list of dicts with "entity" + edges).
    for person_data in persons or []:
        if not isinstance(person_data, dict):
            continue
        entity = person_data.get("entity")
        name = ""
        if entity is not None:
            name = getattr(entity, "name", None) or (
                entity.get("name") if isinstance(entity, dict) else str(entity)
            )
        name = (name or "").strip()
        if not name:
            continue
        decided = person_data.get("decided") or []
        works_on = person_data.get("works_on") or []
        uses = person_data.get("uses") or []
        bits = []
        if works_on:
            bits.append(f"works on {', '.join(str(x) for x in works_on[:3])}")
        if decided:
            bits.append(f"decided on {', '.join(str(x) for x in decided[:3])}")
        if uses:
            bits.append(f"uses {', '.join(str(x) for x in uses[:3])}")
        suffix = " · ".join(bits)
        if suffix:
            lines.append(f"**{name}** — {suffix}")
        else:
            lines.append(f"**{name}**")
        lines.append("")
        rendered_any = True

    if not rendered_any:
        for person in top_people or []:
            if isinstance(person, dict):
                name = (person.get("name") or "").strip()
                role = (person.get("role") or "").strip()
                topic_count = person.get("topic_count")
                expertise = person.get("expertise_topics") or []
            else:
                name = str(person).strip()
                role = ""
                topic_count = None
                expertise = []
            if not name:
                continue
            bits = []
            if role:
                bits.append(role)
            elif expertise:
                bits.append(str(expertise[0]))
            if topic_count is not None:
                bits.append(f"{topic_count} topics")
            suffix = " · ".join(bits)
            if suffix:
                lines.append(f"**{name}** — {suffix}")
            else:
                lines.append(f"**{name}**")
            lines.append("")
            rendered_any = True

    if not rendered_any:
        lines.append(_PLACEHOLDER_LINE)

    content = "\n".join(lines).rstrip() + "\n"
    summary = "Auto-generated contributor list from channel data."
    return content, summary


def _fmt_date(ts: str) -> str:
    """Normalize a timestamp to YYYY-MM-DD; return '' if unparseable."""
    if not ts:
        return ""
    s = str(ts).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return ""


def _activity_fallback(
    recent_facts: list,
    recent_activity_summary: dict,
    clusters: list,
) -> tuple[str, str]:
    """Render a structured Recent Activity page when the LLM output is unusable.

    Produces: summary stats table → facts-per-day chart → daily breakdown
    (grouped by date with author + fact snippet) → top contributors →
    topics-with-recent-activity table. Each section is omitted cleanly when
    its data is empty so short channels don't render placeholders.
    """
    lines: list[str] = []

    # Normalize facts: keep (date, author, text, fact_type) tuples sorted desc.
    rows: list[dict] = []
    for f in recent_facts or []:
        text = (getattr(f, "memory_text", "") or "").strip()
        if not text:
            continue
        date = _fmt_date(getattr(f, "message_ts", ""))
        rows.append(
            {
                "date": date,
                "author": (getattr(f, "author_name", "") or "").strip(),
                "text": text,
                "fact_type": (getattr(f, "fact_type", "") or "").strip().lower(),
            }
        )

    # Aggregate counts.
    from collections import Counter, defaultdict

    per_day: dict[str, int] = defaultdict(int)
    decisions_per_day: dict[str, int] = defaultdict(int)
    authors_counter: Counter = Counter()
    for r in rows:
        if r["date"]:
            per_day[r["date"]] += 1
            if r["fact_type"] == "decision":
                decisions_per_day[r["date"]] += 1
        if r["author"]:
            authors_counter[r["author"]] += 1

    total_facts = len(rows)
    total_decisions = sum(decisions_per_day.values())
    unique_authors = len(authors_counter)
    date_min = min((r["date"] for r in rows if r["date"]), default="")
    date_max = max((r["date"] for r in rows if r["date"]), default="")

    # 1. Summary stats table.
    if total_facts or total_decisions or unique_authors:
        lines.append("## Summary")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Memories added | {total_facts} |")
        lines.append(f"| Decisions | {total_decisions} |")
        lines.append(f"| Contributors | {unique_authors} |")
        if date_min and date_max:
            span = f"{date_min} – {date_max}" if date_min != date_max else date_min
            lines.append(f"| Active period | {span} |")
        lines.append("")

    # 2. Facts-per-day chart (only if we have ≥3 days of data).
    if len(per_day) >= 3:
        import json as _json

        chart_data = [
            {"date": d, "facts": per_day[d], "decisions": decisions_per_day.get(d, 0)}
            for d in sorted(per_day.keys())
        ]
        payload = {
            "type": "area",
            "title": "Knowledge Growth",
            "data": chart_data,
            "xKey": "date",
            "series": ["facts", "decisions"],
        }
        lines.append("## Activity Chart")
        lines.append("")
        lines.append("```chart")
        lines.append(_json.dumps(payload))
        lines.append("```")
        lines.append("")

    # 3. Daily breakdown — group by date, most recent first.
    if rows:
        lines.append("## Daily Breakdown")
        lines.append("")
        by_date: dict[str, list[dict]] = defaultdict(list)
        undated: list[dict] = []
        for r in rows:
            (by_date[r["date"]] if r["date"] else undated).append(r)
        for date in sorted(by_date.keys(), reverse=True):
            lines.append(f"### {date}")
            lines.append("")
            for r in by_date[date][:8]:
                author = r["author"] or "Unknown"
                snippet = r["text"][:180]
                lines.append(f"- **{author}** — {snippet}")
            extra = len(by_date[date]) - 8
            if extra > 0:
                lines.append(f"- _…and {extra} more_")
            lines.append("")
        if undated:
            if by_date:
                lines.append("### Other")
                lines.append("")
            for r in undated[:10]:
                author = r["author"] or "Unknown"
                snippet = r["text"][:180]
                lines.append(f"- **{author}** — {snippet}")
            lines.append("")

    # 4. Top contributors.
    if authors_counter:
        top = authors_counter.most_common(5)
        lines.append("## Top Contributors")
        lines.append("")
        for name, count in top:
            noun = "memory" if count == 1 else "memories"
            lines.append(f"- **{name}** — {count} {noun}")
        lines.append("")

    # 5. Topics with recent activity (from clusters when recent-facts path missing).
    if not rows and clusters:
        cluster_memories = sum(getattr(c, "member_count", 0) or 0 for c in clusters)
        cluster_dates = [_fmt_date(getattr(c, "date_range_end", "")) for c in clusters]
        cluster_dates_valid = [d for d in cluster_dates if d]
        starts = [_fmt_date(getattr(c, "date_range_start", "")) for c in clusters]
        starts_valid = [d for d in starts if d]
        c_min = min(starts_valid + cluster_dates_valid, default="")
        c_max = max(cluster_dates_valid, default="")

        if clusters:
            lines.append("## Summary")
            lines.append("")
            lines.append("| Metric | Value |")
            lines.append("|---|---|")
            lines.append(f"| Topics tracked | {len(clusters)} |")
            lines.append(f"| Total memories | {cluster_memories} |")
            if c_min and c_max:
                span = f"{c_min} – {c_max}" if c_min != c_max else c_min
                lines.append(f"| Active period | {span} |")
            lines.append("")

        lines.append("## Topics with Recent Activity")
        lines.append("")
        lines.append("| Topic | Memories | Last Update |")
        lines.append("|---|---|---|")
        sorted_clusters = sorted(
            clusters,
            key=lambda c: getattr(c, "date_range_end", "") or "",
            reverse=True,
        )
        for c in sorted_clusters[:15]:
            title = (getattr(c, "title", "") or "").strip()
            if not title:
                continue
            mc = getattr(c, "member_count", 0)
            last = _fmt_date(getattr(c, "date_range_end", "")) or "—"
            lines.append(f"| {title} | {mc} | {last} |")
        lines.append("")

    if not lines:
        lines.append(_PLACEHOLDER_LINE)

    content = "\n".join(lines).rstrip() + "\n"
    summary_bits: list[str] = []
    if total_facts:
        summary_bits.append(f"{total_facts} memories")
    if total_decisions:
        summary_bits.append(f"{total_decisions} decisions")
    if unique_authors:
        summary_bits.append(f"{unique_authors} contributors")
    summary = (
        "Recent activity — " + ", ".join(summary_bits) + "."
        if summary_bits
        else "Auto-generated recent activity summary."
    )
    return content, summary


def _resources_fallback(media_data: list[dict]) -> tuple[str, str]:
    """Render a minimal Resources & Media page from media_data."""
    lines: list[str] = ["## Resources & Media", ""]
    rendered_any = False

    # Group by type when easy.
    by_type: dict[str, list[dict]] = {}
    for item in media_data or []:
        if not isinstance(item, dict):
            continue
        t = (item.get("type") or "link").lower()
        if t in {"image", "png", "jpg", "jpeg", "gif"}:
            key = "Images"
        elif t in {"pdf", "doc", "docx", "document", "file"}:
            key = "Documents"
        else:
            key = "Links"
        by_type.setdefault(key, []).append(item)

    for section in ("Images", "Documents", "Links"):
        items = by_type.get(section) or []
        if not items:
            continue
        lines.append(f"### {section}")
        lines.append("")
        for item in items:
            url = (item.get("url") or "").strip()
            if not url:
                continue
            name = (item.get("name") or "").strip()
            if not name:
                name = url[:60]
            lines.append(f"* **{name}** — [open]({url})")
            rendered_any = True
        lines.append("")

    if not rendered_any:
        lines.append(_PLACEHOLDER_LINE)

    content = "\n".join(lines).rstrip() + "\n"
    summary = (
        f"Catalog of {len(media_data)} shared resource(s)."
        if media_data
        else "No shared resources found."
    )
    return content, summary


def _subtopic_fallback(
    sub_title: str,
    sub_facts: list,
    cluster_title: str = "",
) -> tuple[str, str]:
    """Render a minimal subtopic page from its assigned facts."""
    title = (sub_title or "").strip() or "Subtopic"
    lines: list[str] = [f"## {title}", ""]
    rendered_any = False

    for f in sub_facts or []:
        text = (getattr(f, "memory_text", "") or "").strip()
        if not text:
            continue
        text = text[:200]
        lines.append(f"* {text}")
        rendered_any = True

    if not rendered_any:
        blurb = (cluster_title or "").strip()
        if blurb:
            lines.append(f"Part of **{blurb}**. {_PLACEHOLDER_LINE}")
        else:
            lines.append(_PLACEHOLDER_LINE)

    content = "\n".join(lines).rstrip() + "\n"
    summary = f"Auto-generated subtopic summary for {title}."
    return content, summary


_LANG_HEADER_TEMPLATE = """\
## Language Directive (applies to every section below)
The underlying channel memory is in **{source_language}** (BCP-47).
Produce this wiki page's content in **{target_language}** (BCP-47).
- If source_language == target_language, write naturally in that language.
- If they differ, translate from the memory into target_language.
- Preserve proper nouns VERBATIM: people names, project codenames,
  tool/technology names, company names. Do not translate or transliterate
  them. Native-script names (e.g. 阿明) stay in their native script;
  romanized names (e.g. Ah Ming) stay romanized.
- Keep [N] citation markers exactly as they appear. Do not renumber or
  relocate them during translation.
- Keep ```mermaid and ```chart code blocks structurally unchanged; only
  translate the human-readable labels inside them.

---

"""


_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$",
    re.DOTALL,
)

# Patterns that indicate a safety-blocked or refused LLM response.
_SAFETY_PREFIXES = ("I can't", "I cannot", "I'm not able", "As an AI")
_SAFETY_KEYWORDS = ("BLOCKED", "SAFETY")

# GFM separator row: a table row composed only of pipes, dashes, colons, spaces.
_GFM_SEP_ROW_RE = re.compile(r"^\s*\|[\s\-\|:]+\|\s*$")

# Fenced code block pattern for stripping blocks when checking "visuals only".
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


# Directory where failing LLM JSON payloads are dumped for diagnosis.
_PARSE_FAILURE_DUMP_DIR = "/tmp/wiki_parse_failures"


def _dump_parse_failure(raw: str) -> str | None:
    """Write a failing LLM JSON payload to the failure-dump directory.

    Returns the dump path on success, None on any filesystem error (never
    propagates — a dump failure must not break compile).
    """
    try:
        import hashlib
        import os
        import time

        os.makedirs(_PARSE_FAILURE_DUMP_DIR, exist_ok=True)
        ts = int(time.time() * 1000)
        sha = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
        path = os.path.join(_PARSE_FAILURE_DUMP_DIR, f"{ts}_{sha}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        return path
    except Exception:  # noqa: BLE001
        return None


def _recover_truncated_content_field(text: str) -> dict | None:
    """Recover a ``{"content": "..."}`` payload that was truncated mid-string.

    Used when the LLM hit ``max_output_tokens`` while emitting the content
    value — no closing ``"``, no closing ``}``. Locates the ``"content": "``
    opener and treats the rest of the text as the interior, re-escaping raw
    quotes/control-chars so the rebuilt JSON parses cleanly.

    Returns None if the recovered content is shorter than 200 chars (not
    worth the risk of shipping partial garbage) or if no ``content`` key is
    found.
    """
    if not text:
        return None
    key_idx = text.find('"content"')
    if key_idx < 0:
        return None
    colon_idx = text.find(":", key_idx + len('"content"'))
    if colon_idx < 0:
        return None
    open_idx = text.find('"', colon_idx + 1)
    if open_idx < 0:
        return None

    interior = text[open_idx + 1 :]
    # Trim a dangling trailing backslash that would otherwise produce an
    # invalid escape when reconstructed.
    if interior.endswith("\\"):
        interior = interior[:-1]

    if len(interior) < 200:
        return None

    # Re-escape interior char-by-char (same rules as _recover_content_field).
    out: list[str] = []
    k = 0
    n = len(interior)
    while k < n:
        c = interior[k]
        if c == "\\" and k + 1 < n:
            out.append(c)
            out.append(interior[k + 1])
            k += 2
            continue
        if c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        elif c == "\t":
            out.append("\\t")
        elif ord(c) < 0x20:
            out.append(" ")
        else:
            out.append(c)
        k += 1
    escaped_content = "".join(out)

    # Derive summary from first sentence of decoded content.
    try:
        decoded = json.loads('"' + escaped_content + '"')
    except Exception:  # noqa: BLE001
        decoded = interior
    first_line = decoded.lstrip().split("\n", 1)[0].strip()
    first_line = first_line.lstrip("#*>- ").strip()
    summary = ""
    for sep in (". ", "! ", "? "):
        if sep in first_line:
            summary = first_line.split(sep, 1)[0] + sep.strip()
            break
    else:
        summary = first_line[:200]
    summary = json.dumps(summary)[1:-1]

    reconstructed = '{"content": "' + escaped_content + '", "summary": "' + summary + '"}'
    try:
        return json.loads(reconstructed)
    except json.JSONDecodeError:
        return None


def _recover_content_field(raw: str) -> dict | None:
    """Aggressive recovery of a ``{"content": "...", ...}`` LLM payload.

    Used as a last-chance fallback when the standard JSON parsers fail —
    most commonly because the LLM emitted an unescaped ``"`` or raw control
    char inside the content string literal. Locates the ``"content"`` key,
    finds the outer object's closing ``}``, and walks backwards to identify
    the closing ``"`` of the content value (the last ``"`` that is followed
    only by whitespace + ``,`` or ``}``). Re-escapes the interior so it
    can be re-parsed as JSON.

    Returns the parsed dict or None if no recovery is possible.
    """
    if not raw:
        return None
    text = raw.strip()
    truncated = False
    if not text.endswith("}"):
        # Find the last closing brace of the outer object.
        last_brace = text.rfind("}")
        if last_brace < 0:
            # No closing brace anywhere — likely truncated mid-content-string.
            # Take the raw text as-is for truncation branch below.
            truncated = True
        else:
            text = text[: last_brace + 1]

    if truncated:
        return _recover_truncated_content_field(text)

    # Locate the "content" key.
    key_idx = text.find('"content"')
    if key_idx < 0:
        return None
    # Find the ':' after the key.
    colon_idx = text.find(":", key_idx + len('"content"'))
    if colon_idx < 0:
        return None
    # Find the opening '"' of the value.
    open_idx = text.find('"', colon_idx + 1)
    if open_idx < 0:
        return None

    last_brace = text.rfind("}")
    if last_brace <= open_idx:
        return None
    close_idx = -1
    # Preferred heuristic: find the first `"`, `"` followed by the next
    # top-level key pattern (`,\s*"<identifier>"\s*:`). Anything before that
    # is still inside the content value even if stray quotes exist.
    _next_key_re = re.compile(r'"\s*,\s*"[A-Za-z_][A-Za-z0-9_]*"\s*:')
    m = _next_key_re.search(text, open_idx + 1)
    if m and m.start() < last_brace:
        close_idx = m.start()
    # Fallback: walk backwards from the last '}' to find the closing '"'.
    i = last_brace - 1
    while close_idx < 0 and i > open_idx:
        ch = text[i]
        if ch == '"':
            # Check what follows (whitespace then , or })
            j = i + 1
            while j < last_brace and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in ",}":
                close_idx = i
                break
        i -= 1
    if close_idx < 0:
        # Fallback: the outer object may have only the content key.
        # Try: last '"' before the final '}'.
        j = last_brace - 1
        while j > open_idx and text[j] in " \t\r\n":
            j -= 1
        if j > open_idx and text[j] == '"':
            close_idx = j
    if close_idx <= open_idx:
        return None

    interior = text[open_idx + 1 : close_idx]
    # Re-escape interior char-by-char.
    out: list[str] = []
    k = 0
    n = len(interior)
    while k < n:
        c = interior[k]
        if c == "\\" and k + 1 < n:
            # Preserve existing escape sequences verbatim.
            out.append(c)
            out.append(interior[k + 1])
            k += 2
            continue
        if c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        elif c == "\t":
            out.append("\\t")
        elif ord(c) < 0x20:
            out.append(" ")
        else:
            out.append(c)
        k += 1
    escaped_content = "".join(out)

    # Try to salvage the original "summary" field if present & parseable.
    summary = ""
    # Look for "summary" after close_idx.
    tail = text[close_idx + 1 :]
    sm_key = tail.find('"summary"')
    if sm_key >= 0:
        sm_colon = tail.find(":", sm_key + len('"summary"'))
        if sm_colon >= 0:
            sm_open = tail.find('"', sm_colon + 1)
            if sm_open >= 0:
                # Find closing '"' of summary value — simple scan respecting escapes.
                j = sm_open + 1
                while j < len(tail):
                    if tail[j] == "\\" and j + 1 < len(tail):
                        j += 2
                        continue
                    if tail[j] == '"':
                        summary = tail[sm_open + 1 : j]
                        break
                    j += 1
    # Fallback: first sentence of recovered content (unescape for summary).
    if not summary:
        try:
            # Use the re-escaped content for JSON decode of just the string.
            decoded = json.loads('"' + escaped_content + '"')
        except Exception:  # noqa: BLE001
            decoded = escaped_content
        first_line = decoded.lstrip().split("\n", 1)[0].strip()
        first_line = first_line.lstrip("#*>- ").strip()
        for sep in (". ", "! ", "? "):
            if sep in first_line:
                summary = first_line.split(sep, 1)[0] + sep.strip()
                break
        else:
            summary = first_line[:200]
        # Re-escape summary for reconstruction.
        summary = json.dumps(summary)[1:-1]

    reconstructed = '{"content": "' + escaped_content + '", "summary": "' + summary + '"}'
    try:
        return json.loads(reconstructed)
    except json.JSONDecodeError:
        return None


def _escape_control_chars_inside_strings(text: str) -> str:
    """Escape raw control characters found inside JSON string literals.

    Walks the candidate string tracking in/out-of-string state. Inside a
    string literal, replaces literal control chars with their JSON-safe
    escape sequences. This recovers JSON that Gemini sometimes emits with
    raw newlines / tabs inside "content" values.
    """
    result: list[str] = []
    in_string = False
    i = 0
    n = len(text)
    _ESCAPE_MAP = {
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\x00": " ",
        "\x0b": " ",
        "\x0c": " ",
    }
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\":
                # Consume escaped char verbatim (two chars).
                result.append(ch)
                i += 1
                if i < n:
                    result.append(text[i])
                    i += 1
                continue
            if ch == '"':
                in_string = False
                result.append(ch)
                i += 1
                continue
            escaped = _ESCAPE_MAP.get(ch)
            if escaped:
                result.append(escaped)
            else:
                result.append(ch)
        else:
            if ch == '"':
                in_string = True
            result.append(ch)
        i += 1
    return "".join(result)


def _is_degenerate_content(content: str) -> tuple[bool, str]:
    """Detect degenerate LLM output (too short, low alnum ratio, dash-wall, visuals-only).

    Returns (is_degenerate, reason).
    """
    if len(content) < 80:
        return True, "too short"
    total = max(len(content), 1)
    alnum_count = sum(1 for c in content if c.isalnum())
    if alnum_count / total < 0.2:
        return True, "low alnum ratio"
    # Check for >= 5 consecutive GFM separator rows (dash-wall).
    lines = content.splitlines()
    max_run = run = 0
    for line in lines:
        if _GFM_SEP_ROW_RE.match(line):
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    if max_run >= 5:
        return True, "dash wall"
    # Check if stripping fenced blocks leaves no alphanumeric content.
    stripped = _FENCED_BLOCK_RE.sub("", content)
    if not any(c.isalnum() for c in stripped):
        return True, "visuals only"
    return False, ""


_DELIMITED_RESPONSE_SUFFIX = """

Return your response in this exact format, nothing else:

###SUMMARY###
<one or two sentence summary, plain text, no markdown>
###CONTENT###
<full markdown content as specified above>
###END###
"""


def _parse_delimited_response(raw: str) -> CompiledPageContent:
    """Parse a delimited LLM response into CompiledPageContent.

    Tolerates:
    - Missing ###END### marker (takes everything after ###CONTENT###).
    - Missing ###SUMMARY### marker (derives summary from first sentence of content).
    - Preamble before ###SUMMARY### (ignored).
    - Echoed ###CONTENT### / ###END### markers inside the body (uses rsplit to
      keep trailing body content, treating earlier occurrences as part of prose).

    On total failure (no ###CONTENT### at all), returns empty CompiledPageContent
    so the existing empty-retry logic kicks in.
    """
    if not raw:
        return CompiledPageContent(content="", summary="")

    if "###CONTENT###" not in raw:
        return CompiledPageContent(content="", summary="")

    # rsplit on ###CONTENT###: head = everything before the LAST ###CONTENT### marker
    # (may contain ###SUMMARY### + summary text, plus any echoed markers);
    # tail = the actual content body (which may itself contain echoed ###CONTENT###).
    head, _, tail = raw.rpartition("###CONTENT###")

    # Strip trailing ###END### marker from tail using rsplit to be tolerant of
    # echoed ###END### inside the body — only the LAST occurrence is the real terminator.
    if "###END###" in tail:
        tail, _, _ = tail.rpartition("###END###")
    content = tail.strip()

    # Extract summary from head: find LAST ###SUMMARY### marker.
    summary = ""
    if "###SUMMARY###" in head:
        _, _, summary_part = head.rpartition("###SUMMARY###")
        summary = summary_part.strip()

    # Fallback: derive summary from first sentence of content if missing.
    if not summary and content:
        first_line = content.lstrip().split("\n", 1)[0].strip()
        # Strip leading markdown-ish prefixes for a cleaner summary.
        first_line_clean = first_line.lstrip("#*>- ").strip()
        # First sentence from first non-empty line.
        for sep in (". ", "! ", "? "):
            if sep in first_line_clean:
                summary = first_line_clean.split(sep, 1)[0] + sep.strip()
                break
        else:
            summary = first_line_clean

    return CompiledPageContent(content=content, summary=summary)


def _is_safety_block(raw: str) -> bool:
    """Return True if the raw LLM response looks like a safety refusal."""
    head = raw[:200]
    if any(raw.startswith(p) for p in _SAFETY_PREFIXES):
        return True
    return any(kw in head for kw in _SAFETY_KEYWORDS)


def _parse_llm_json(raw: str | None) -> dict | list | None:
    """Parse an LLM JSON response tolerantly.

    Handles the common failure modes that block Cantonese/CJK wiki
    generation: markdown-fenced JSON (```json ... ```), leading/trailing
    prose, and truncation. Returns a parsed object or None on failure.
    """
    if not raw:
        return None
    text = raw.strip()

    # Strip a surrounding ```json ... ``` fence if present.
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Fast path.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Cut to the outermost JSON object/array span and retry.
    first_brace = min(
        (i for i in (text.find("{"), text.find("[")) if i >= 0),
        default=-1,
    )
    last_brace = max(text.rfind("}"), text.rfind("]"))
    if first_brace >= 0 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        # Control-char sanitizer: escape raw control chars inside string literals
        # and retry. Gated on wiki_parse_hardening flag.
        try:
            from beever_atlas.infra.config import get_settings

            if get_settings().wiki_parse_hardening:
                sanitized = _escape_control_chars_inside_strings(candidate)
                try:
                    return json.loads(sanitized)
                except json.JSONDecodeError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        # Content-field aggressive recovery (unescaped quotes / control chars
        # inside the "content" string literal). Gated on wiki_parse_hardening.
        try:
            from beever_atlas.infra.config import get_settings

            if get_settings().wiki_parse_hardening:
                recovered = _recover_content_field(candidate)
                if recovered is not None:
                    return recovered
        except Exception:  # noqa: BLE001
            pass
        # Last resort: reuse the ingestion-side truncation recovery.
        try:
            from beever_atlas.services.json_recovery import recover_truncated_json

            return recover_truncated_json(candidate)
        except Exception:  # noqa: BLE001
            return None
    return None


def _looks_like_handle(s: str) -> bool:
    """Heuristic: does this string look like a protected identifier rather than a
    natural-language title? Used to reject LLM "translations" of things like
    ``@alice``, ``build-pipeline``, ``v2``, ``FooBar``, ``foo_bar`` — while still
    letting common single-word nouns (``Meeting``, ``Roadmap``) translate.
    """
    if not s or not s.isascii() or any(ch.isspace() for ch in s):
        return False
    if s.startswith(("@", "#", "/")):
        return True
    if any(ch in s for ch in "_-./"):
        return True
    if any(ch.isdigit() for ch in s):
        return True
    # Internal capitals (CamelCase, iOS) — but a plain Title-cased word
    # like "Meeting" has its only capital at index 0, so skip that case.
    caps = [i for i, ch in enumerate(s) if ch.isupper()]
    return len(caps) >= 2 and caps != [0]


class WikiCompiler:
    """Compiles gathered channel data into WikiPage objects using the LLM."""

    def __init__(
        self,
        *,
        target_lang: str = "en",
        source_lang: str = "en",
    ) -> None:
        provider = get_llm_provider()
        self._model_name: str = provider.get_model_string("wiki_compiler")
        self._target_lang = target_lang
        self._source_lang = source_lang

    def _fmt_prompt(self, template: str, **kwargs) -> str:
        """Format a wiki page prompt with language header prepended.

        Every page prompt is prefixed with the language directive so the LLM
        renders in `target_lang` while preserving proper nouns from
        `source_lang` memory. Template placeholders remain unchanged.
        """
        header = _LANG_HEADER_TEMPLATE.format(
            target_language=self._target_lang,
            source_language=self._source_lang,
        )
        return header + template.format(**kwargs)

    def _page_title(self, page_id: str) -> str:
        lang_map = WIKI_PAGE_TITLES.get(self._target_lang) or WIKI_PAGE_TITLES["en"]
        return lang_map.get(page_id) or WIKI_PAGE_TITLES["en"].get(page_id, page_id.title())

    @staticmethod
    def _is_topic_relevant(
        cluster,
        channel_themes: list[str],
        cluster_facts: dict,
        total_cluster_count: int = 8,
        min_facts_override: int | None = None,
    ) -> tuple[bool, str]:
        """Check if a topic cluster should get its own page.

        Returns (should_include, skip_reason) tuple.

        Two-tier policy based on channel sparsity (total_cluster_count):
        - Sparse channels (< 8 clusters): keep any topic that clears the
          per-channel min-facts threshold, regardless of tag overlap, so
          small channels still render all topics.
        - Dense channels (≥ 8 clusters): keep topics that clear the
          threshold AND (≥5 facts OR tag overlap with channel themes).

        ``min_facts_override`` lets the caller inject the tiered threshold
        resolved once per compile run (see ``_resolve_topic_compile_threshold``);
        callers that pass ``None`` get the legacy constant for back-compat with
        ad-hoc test harnesses.
        """
        member_count = len(cluster_facts.get(cluster.id, []))

        min_facts = (
            min_facts_override if min_facts_override is not None else TOPIC_MIN_MEMORY_THRESHOLD
        )
        # Check minimum memory threshold (both tiers)
        if member_count < min_facts:
            return (
                False,
                f"{member_count} facts, below minimum threshold of {min_facts}",
            )

        # Sparse channel: any topic with ≥3 facts is kept unconditionally
        if total_cluster_count < 8:
            return True, ""

        # Dense channel: keep if popular (≥5 facts) without needing tag overlap.
        # Soft-concern-1 restore — only the SPARSE-channel branch was meant to
        # relax; the dense threshold stays at the original ≥5 boundary.
        if member_count >= 5:
            return True, ""

        # Dense channel, 3-4 facts: require tag overlap with channel themes
        # Normalize for comparison
        cluster_tags = {t.lower().strip() for t in (cluster.topic_tags or [])}
        theme_words = set()
        for theme in channel_themes or []:
            for word in theme.lower().replace("-", " ").replace("_", " ").split():
                if len(word) > 2:
                    theme_words.add(word)

        # Check if any cluster tag word overlaps with any theme word
        cluster_words = set()
        for tag in cluster_tags:
            for word in tag.replace("-", " ").replace("_", " ").split():
                if len(word) > 2:
                    cluster_words.add(word)

        if cluster_words & theme_words:
            return True, ""

        return False, f"no tag overlap with channel themes and only {member_count} facts"

    # ── Content post-processing ────────────────────────────────────────

    _SOURCES_RE = re.compile(r"\n*#{2,4}\s*Sources?\s*\n[\s\S]*$")
    _CITATION_LIST_RE = re.compile(r"\n+(?:- \[\d+\] [^\n]+\n?){2,}\s*$")
    _MERMAID_BLOCK_RE = re.compile(r"(```mermaid\s*\n)([\s\S]*?)(```)")
    _EDGE_LABEL_RE = re.compile(r"--\s+[^-\n][^>\n]*?\s+-->")
    _BLANK_LINES_RE = re.compile(r"\n{4,}")
    # Matches 4+ consecutive inline citation markers like [1][2][5][6][8]...
    _OVERCITATION_RE = re.compile(r"(?:\[\d+\]\s*){4,}")
    # Source-list entries we need to validate against used [N] in body.
    # Matches a single "- [N] ..." line inside the Sources/citations list.
    _SOURCE_ENTRY_RE = re.compile(r"^- \[(\d+)\] [^\n]+$", re.MULTILINE)
    # Inline citation markers in body text: [1], [12].
    _INLINE_CITATION_RE = re.compile(r"\[(\d+)\]")

    @staticmethod
    def _auto_close_unclosed_mermaid(content: str) -> str:
        """Append a closing ``` to any ```mermaid block that never closes.

        The main `_MERMAID_BLOCK_RE` substitution only matches well-formed
        blocks; if the LLM truncates output mid-diagram, the opening fence
        passes through silently and the rest of the page renders as raw
        mermaid source. This helper scans for orphan openers and emits a
        closer so the diagram at least terminates cleanly.
        """
        if "```mermaid" not in content:
            return content
        # Walk fence-by-fence. A mermaid opener is unclosed if no ``` follows
        # it before the next ```mermaid or end of string.
        parts: list[str] = []
        i = 0
        length = len(content)
        while i < length:
            opener_idx = content.find("```mermaid", i)
            if opener_idx < 0:
                parts.append(content[i:])
                break
            # Emit everything up to and including the opener + newline.
            # Find end of opener line.
            opener_end = content.find("\n", opener_idx)
            if opener_end < 0:
                # Opener is the last line — just append a newline + closer.
                parts.append(content[i:] + "\n```\n")
                i = length
                break
            parts.append(content[i : opener_end + 1])
            i = opener_end + 1
            # Search for the next ``` that closes this block, stopping if we
            # hit another ```mermaid first (which means the first was unclosed).
            close_idx = content.find("```", i)
            next_mermaid_idx = content.find("```mermaid", i)
            if close_idx < 0:
                # No closer at all — emit remaining body + close fence.
                parts.append(content[i:].rstrip("\n") + "\n```\n")
                i = length
                break
            if next_mermaid_idx >= 0 and next_mermaid_idx <= close_idx:
                # Another mermaid block starts before any closer — the first
                # is unclosed. Emit body up to next opener + synthetic closer.
                parts.append(content[i:next_mermaid_idx].rstrip("\n") + "\n```\n\n")
                i = next_mermaid_idx
                continue
            # Well-formed block — emit body + closer + trailing newline.
            parts.append(content[i : close_idx + 3])
            i = close_idx + 3
        return "".join(parts)

    @classmethod
    def _filter_citations_to_body(
        cls, content: str, citations: list["WikiCitation"]
    ) -> list["WikiCitation"]:
        """Drop citations whose `[N]` marker never appears in `content`.

        The renderer re-attaches the WikiPage.citations list as a visible
        Sources section, so the earlier `_strip_orphan_citations` pass
        (which only operates on LLM body text) cannot remove entries that
        were trimmed from the body. This helper runs after
        `_postprocess_content` and filters the citations list by the set
        of `[N]` indices actually referenced in the final body.

        A citation is kept when its id is `[N]` and the string `[N]`
        appears somewhere in `content`. Ids that don't match `[N]` are
        kept unchanged (defensive).
        """
        if not content or not citations:
            return citations
        used_indices: set[int] = set()
        # Match BOTH single ``[N]`` and comma-grouped ``[N, M, ...]``
        # forms so a body that uses ``[1, 2]`` is recognised as
        # referencing both citation 1 AND citation 2. Without this, a
        # comma-grouped reference is silently invisible to the filter
        # and ``_filter_citations_to_body`` returns the unfiltered list
        # (because ``used_indices`` ends up empty), inflating the
        # rendered Sources panel with orphan citations.
        _grouped_re = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
        for m in _grouped_re.finditer(content):
            for part in m.group(1).split(","):
                try:
                    used_indices.add(int(part.strip()))
                except ValueError:
                    pass
        if not used_indices:
            return citations
        kept: list["WikiCitation"] = []
        for c in citations:
            cid = getattr(c, "id", "") or ""
            mm = re.match(r"^\[(\d+)\]$", cid)
            if mm is None:
                kept.append(c)
                continue
            try:
                if int(mm.group(1)) in used_indices:
                    kept.append(c)
            except ValueError:
                kept.append(c)
        return kept

    @classmethod
    def _strip_out_of_range_inline_citations(cls, content: str, max_index: int) -> str:
        """Drop `[N]` markers where N exceeds max_index (the count of citation facts).

        The LLM occasionally emits citation numbers beyond the provided
        citation list (e.g. references `[36]` when only 12 facts were
        supplied). `_filter_citations_to_body` drops these from the citations
        list, but the inline markers remain in the body as dangling refs.
        This pass removes them. Comma-separated groups like `[1, 36, 7]` are
        reduced to `[1, 7]`; groups that become empty are removed entirely,
        along with a preceding space.
        """
        if not content or max_index <= 0:
            return content

        def _replace(match: re.Match) -> str:
            raw = match.group(0)
            inner = match.group(1)
            parts = [p.strip() for p in inner.split(",")]
            kept: list[str] = []
            for p in parts:
                if not p.isdigit():
                    kept.append(p)
                    continue
                if 1 <= int(p) <= max_index:
                    kept.append(p)
            if not kept:
                return ""
            if len(kept) == len(parts):
                return raw
            return "[" + ", ".join(kept) + "]"

        # Match either single `[N]` or comma-grouped `[N, M, ...]`.
        pattern = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
        out = pattern.sub(_replace, content)
        # Collapse ` ` left by removed markers (e.g. "fact  ." -> "fact.")
        out = re.sub(r" {2,}([.,;:])", r"\1", out)
        out = re.sub(r"  +", " ", out)
        return out

    @classmethod
    def _strip_orphan_citations(cls, content: str) -> str:
        """Remove Source list entries whose [N] marker is never cited in body.

        Some LLM outputs define sources like `- [6] @Author — …` but never
        reference `[6]` in the prose. This leaves "dangling" entries that add
        noise and confuse readers. This pass is conservative: it only acts
        when the Sources list is a trailing markdown block prefixed by a
        heading `### Sources` or `## Sources` (matching _SOURCES_RE shape, but
        we preserve the list here instead of stripping it).

        Note: _SOURCES_RE already runs earlier in _postprocess_content and
        strips the whole Sources section by design (because the UI renders
        citations from WikiPage.citations, not the inline list). This
        fallback handles cases where a Sources list leaked past the regex
        (e.g., inside the body rather than trailing).
        """
        if "[" not in content:
            return content
        # Collect all inline citation indices used in non-source-list lines.
        # Skip lines that are themselves source entries (`- [N] @author — …`),
        # otherwise `- [6] @claire` counts as a "use" of [6] and the orphan
        # never gets removed.
        lines = content.split("\n")
        used: set[int] = set()
        in_sources = False
        for line in lines:
            stripped = line.strip()
            if re.match(r"^#{2,4}\s*Sources?\s*$", stripped, re.IGNORECASE):
                in_sources = True
                continue
            if in_sources and stripped.startswith("#"):
                in_sources = False
            if in_sources:
                continue
            # Skip source-entry lines themselves.
            if cls._SOURCE_ENTRY_RE.match(line):
                continue
            for m in cls._INLINE_CITATION_RE.finditer(line):
                try:
                    used.add(int(m.group(1)))
                except ValueError:
                    pass
        if not used:
            return content
        # Drop source-list lines whose [N] is not in `used`.
        kept_lines: list[str] = []
        for line in lines:
            src_match = cls._SOURCE_ENTRY_RE.match(line)
            if src_match:
                try:
                    idx = int(src_match.group(1))
                except ValueError:
                    kept_lines.append(line)
                    continue
                if idx not in used:
                    continue
            kept_lines.append(line)
        return "\n".join(kept_lines)

    @staticmethod
    def _postprocess_content(content: str) -> str:
        """Clean LLM output before storing as WikiPage content."""
        if not content:
            return content

        # 1. Strip terminal ## Sources / ### Sources sections
        content = WikiCompiler._SOURCES_RE.sub("", content)

        # 1b. Strip terminal numbered citation lists (e.g., "- [1] @Author ...")
        content = WikiCompiler._CITATION_LIST_RE.sub("", content)

        # 1c. Strip orphan source-list entries whose [N] never appears in body.
        # Second line of defence after _SOURCES_RE for non-trailing source lists.
        content = WikiCompiler._strip_orphan_citations(content)

        # 1d. Strip bracketed UUID / fact-id citation markers the LLM
        # sometimes emits as literal prose (e.g.,
        # ``[05d0b2e3-..., e6abd025-...]`` or ``[f_abc123, f_xyz789]``).
        # The expected citation form is the digit-bracket ``[1, 2]``
        # marker that the renderer turns into chips, OR the ``f_xxx``
        # form on narrative pages. Anything ELSE inside ``[...]`` that
        # looks like a UUID / hex hash / ``f_``-prefixed id is a prompt
        # leak — strip it (and the trailing space if it was preceded
        # by one) so the prose reads cleanly.
        content = re.sub(
            r"\s*\[(?:"
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            r"|f_[A-Za-z0-9]+"
            r")(?:\s*,\s*(?:"
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            r"|f_[A-Za-z0-9]+"
            r"))*\]",
            "",
            content,
        )

        # 1d. Auto-close unclosed ```mermaid fences. Must run BEFORE the
        # _MERMAID_BLOCK_RE substitution below, otherwise orphan openers
        # pass through unmodified and break downstream rendering.
        content = WikiCompiler._auto_close_unclosed_mermaid(content)

        # 2. Sanitize mermaid blocks
        def _sanitize_node_label(match: re.Match) -> str:
            node_id = match.group(1)
            label = match.group(2)
            # Strip characters mermaid rejects inside [...]: parens, quotes,
            # backticks, plus dots and slashes (which the 11.x flowchart parser
            # rejects in labels like `app.example.com`, `CI/CD pipeline`).
            label = re.sub(r'[()"\'\`./]', " ", label)
            # Collapse repeated spaces
            label = re.sub(r" {2,}", " ", label).strip()
            # If label is now empty, fall back to node ID so the box shows something
            if not label:
                label = node_id
            return f"{node_id}[{label}]"

        def _clean_mermaid(m: re.Match) -> str:
            opener, body, closer = m.group(1), m.group(2), m.group(3)
            lines = body.split("\n")
            cleaned: list[str] = []
            for line in lines:
                stripped = line.strip()
                # Remove forbidden directives
                if stripped.startswith(("subgraph", "end", "style ", "classDef ", "class ")):
                    continue
                # Drop lines that are purely an empty bracket node: ID[] or bare [Label]
                if re.match(r"^\s*\w*\[\s*\]\s*$", line):
                    continue
                # Convert dash-space edge labels to pipe style: A -- label --> B  →  A -->|label| B
                line = re.sub(r"--\s+([^-\n][^>\n]*?)\s+-->", r"-->|\1|", line)
                line = re.sub(r"--\s+([^-\n][^-\n]*?)\s+---", r"---|\1|", line)
                # Normalize rare arrow endings `--x` / `--o` (cross/circle) which
                # the mermaid 11.x flowchart parser rejects when combined with
                # pipe labels. Convert to the standard `-->` so labels render.
                line = re.sub(r"--x\s*\|", "-->|", line)
                line = re.sub(r"--o\s*\|", "-->|", line)
                # Strip colon-style labels conservatively: only when --> NODE: free text (no brackets)
                line = re.sub(r"(-->\s*\w+(?:\[[^\]]*\])?)\s*:\s*[^\[\]|]+$", r"\1", line)
                # Sanitize node-definition labels: strip forbidden chars inside [...]
                line = re.sub(r"([A-Za-z0-9_]+)\[([^\]]*)\]", _sanitize_node_label, line)
                # Keep pipe-style labels intact: A -->|label| B is valid mermaid
                cleaned.append(line)
            return opener + "\n".join(cleaned) + closer

        content = WikiCompiler._MERMAID_BLOCK_RE.sub(_clean_mermaid, content)

        # 2b. Prune edges referencing undefined nodes inside mermaid blocks.
        # Example: "EEACBA -->|drives| ELLMS" where EEACBA is never defined as
        # "EEACBA[Label]" elsewhere in the block — a typo of EACBA. Gated on
        # wiki_parse_hardening so legacy behaviour is preserved when disabled.
        try:
            from beever_atlas.infra.config import get_settings

            if get_settings().wiki_parse_hardening:
                content = WikiCompiler._MERMAID_BLOCK_RE.sub(
                    WikiCompiler._prune_undefined_mermaid_edges, content
                )
        except Exception:  # noqa: BLE001
            pass

        # 3. Trim over-citation: keep at most 3 consecutive [N] markers per cluster
        def _trim_citations(m: re.Match) -> str:
            markers = re.findall(r"\[\d+\]", m.group(0))
            return "".join(markers[:3])

        content = WikiCompiler._OVERCITATION_RE.sub(_trim_citations, content)

        # 4. Collapse 3+ consecutive blank lines to 2
        content = WikiCompiler._BLANK_LINES_RE.sub("\n\n\n", content)

        return content.rstrip() + "\n"

    # Matches any node definition with a bracketed label:
    # ID[Label], ID(Label), ID{Label}, ID((Label)). Captures the ID.
    _NODE_DEF_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[\[|\[|\(\(|\(|\{)")
    # Matches edge lines with optional pipe-style label:
    #   SRC --> DST, SRC -->|label| DST, SRC --- DST, SRC ---|label| DST
    # Also tolerates bracketed labels on SRC/DST (e.g. SRC[Foo] -->|x| DST[Bar]).
    _EDGE_LINE_RE = re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s*(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\}))?"
        r"\s*(?:-->|---)(?:\s*\|[^|]*\|)?\s*"
        r"([A-Za-z_][A-Za-z0-9_]*)(?:\s*(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\}))?\s*$"
    )

    @staticmethod
    def _prune_undefined_mermaid_edges(m: "re.Match[str]") -> str:
        """Drop edge lines whose SRC or DST references an undefined node.

        A node is "defined" if it appears anywhere in the same fenced block as
        ``ID[Label]`` / ``ID(Label)`` / ``ID{Label}`` / ``ID((Label))`` — i.e.
        it has a bracketed label attached. Edges that reference an identifier
        that was never given a label (typos like ``EEACBA --> ELLMS``) are
        silently dropped. Node-definition lines and non-edge lines (e.g.
        ``graph TD``) are left untouched.
        """
        opener, body, closer = m.group(1), m.group(2), m.group(3)
        lines = body.split("\n")

        # First pass: collect all defined node IDs (those appearing with a
        # bracketed label anywhere in the block).
        defined: set[str] = set()
        for line in lines:
            for node_id in WikiCompiler._NODE_DEF_RE.findall(line):
                defined.add(node_id)

        # Second pass: drop edges whose endpoints are undefined.
        kept: list[str] = []
        for line in lines:
            edge_match = WikiCompiler._EDGE_LINE_RE.match(line)
            if edge_match is None:
                kept.append(line)
                continue
            src, dst = edge_match.group(1), edge_match.group(2)
            if src not in defined or dst not in defined:
                logger.info(
                    "WikiCompiler: dropped edge with undefined node ref: %s",
                    line.strip(),
                )
                continue
            kept.append(line)

        return opener + "\n".join(kept) + closer

    @staticmethod
    def _filter_media_for_resources(media_data: list[dict]) -> list[dict]:
        """Filter media items for the Resources page — remove noise, cap per domain."""
        # Shortener domains to exclude
        shortener_hosts = {"t.co", "bit.ly", "tinyurl.com", "goo.gl", "ow.ly"}
        # Generic names to exclude
        generic_names = {"image.png", "download", "shortened link", "image.jpg", "image.jpeg"}

        filtered: list[dict] = []
        for item in media_data:
            url = item.get("url", "")
            name = (item.get("name", "") or "").strip().lower()

            # Skip shorteners
            try:
                from urllib.parse import urlparse

                host = urlparse(url).hostname or ""
                if any(host.endswith(s) for s in shortener_hosts):
                    continue
            except Exception as exc:
                logger.debug(
                    "_filter_media_data: urlparse failed url=%r: %s", url, exc, exc_info=False
                )

            # Skip generic names
            if name in generic_names:
                continue

            filtered.append(item)

        # Domain-based capping. Canonicalize twitter.com→x.com so both
        # variants share a cap (preventing 5 x.com + 5 twitter.com = 10
        # near-duplicates of the same platform).
        from collections import Counter

        _SOCIAL_DOMAINS = {
            "x.com",
            "twitter.com",
            "threads.com",
            "facebook.com",
            "instagram.com",
            "linkedin.com",
        }
        domain_counts: Counter[str] = Counter()
        # Second-pass normalized-URL dedup so the filter collapses items that
        # slipped past _build_media_data (e.g., media from different callers).
        seen_normalized: set[str] = set()
        domain_capped: list[dict] = []
        for item in filtered:
            url = item.get("url", "") or ""
            norm = _normalize_url(url)
            if norm and norm in seen_normalized:
                continue
            if norm:
                seen_normalized.add(norm)
            try:
                from urllib.parse import urlparse

                host = (urlparse(url).hostname or "").lower()
                if host.startswith("www."):
                    host = host[4:]
                # Canonicalize twitter.com → x.com for cap purposes.
                if host == "twitter.com" or host.endswith(".twitter.com"):
                    host = "x.com"
                domain = host
            except Exception as exc:
                logger.debug(
                    "_filter_media_data: domain parse failed url=%r: %s", url, exc, exc_info=False
                )
                domain = "unknown"
            if domain in _SOCIAL_DOMAINS:
                cap = 5
            elif domain == "github.com" or domain.endswith(".github.com"):
                # CodeQL alert #33: `domain` is already a parsed hostname
                # (line ~2567), so substring `"github.com" in domain` is
                # redundant AND would match `github.com.evil.com`. Use
                # exact-match-or-proper-subdomain instead.
                cap = 10
            else:
                cap = 5
            if domain_counts[domain] < cap:
                domain_capped.append(item)
                domain_counts[domain] += 1

        # Total cap
        return domain_capped[:30]

    # ── LLM call ─────────────────────────────────────────────────────

    # Per-page-kind output token budgets (Phase 3). Sized at 50% headroom
    # over observed max for each kind. Resources needs 32k for the full
    # 40-row media table; topic/subtopic at 12k covers the longest seen
    # pages; smaller fixed pages are capped conservatively.
    # When wiki_token_budget_v2=OFF, the uniform legacy 32k applies.
    _PAGE_KIND_MAX_TOKENS: dict[str, int] = {
        "resources": 32768,
        "topic": 12288,
        "subtopic": 12288,
        "overview": 10240,
        "people": 8192,
        "decisions": 8192,
        "activity": 8192,
        "glossary": 12288,
        "faq": 12288,
        "analysis": 8192,
        "translation": 4096,
    }
    _PAGE_KIND_MAX_TOKENS_DEFAULT = 16384

    async def _llm_generate_json(
        self,
        prompt: str,
        temperature: float = 0.2,
        page_kind: str = "topic",
    ) -> str:
        """Call the configured LLM and return raw text. Supports Gemini and Ollama."""
        from beever_atlas.infra.config import get_settings

        settings = get_settings()
        if settings.wiki_token_budget_v2:
            max_tokens = self._PAGE_KIND_MAX_TOKENS.get(
                page_kind, self._PAGE_KIND_MAX_TOKENS_DEFAULT
            )
        else:
            max_tokens = 32768

        # Phase 5: delimited response mode. Markdown-content pages switch to a
        # delimited format when wiki_compiler_v2=ON. Analysis and translation
        # always use JSON mode (invariant) because their responses are consumed
        # programmatically as lists/dicts with non-string values.
        use_delimited = settings.wiki_compiler_v2 and page_kind not in {"analysis", "translation"}

        if is_ollama_model(self._model_name):
            import os

            from beever_atlas.services.llm_dispatch import dispatch_completion

            os.environ.setdefault("OLLAMA_API_BASE", settings.ollama_api_base)
            if use_delimited:
                resp = await dispatch_completion(
                    provider="ollama",
                    model=self._model_name,
                    messages=[{"role": "user", "content": prompt + _DELIMITED_RESPONSE_SUFFIX}],
                    temperature=temperature,
                )
            else:
                resp = await dispatch_completion(
                    provider="ollama",
                    model=self._model_name,
                    messages=[
                        {"role": "user", "content": prompt + "\n\nRespond with valid JSON only."}
                    ],
                    temperature=temperature,
                    format="json",
                )
            return resp.choices[0].message.content or "{}"  # pyright: ignore[reportAttributeAccessIssue]
        else:
            from beever_atlas.services.llm_dispatch import (
                dispatch_completion,
                normalize_litellm_model,
                sniff_provider,
            )

            if use_delimited:
                # Plain-text completion — the delimited response suffix
                # instructs the model to emit a custom-delimited payload that
                # ``_parse_llm_json`` will unpack. No JSON mode requested.
                kwargs: dict[str, Any] = {}
                content = prompt + _DELIMITED_RESPONSE_SUFFIX
            else:
                # response_format=json_object nudges Gemini (via LiteLLM) toward
                # valid JSON without forcing a schema. A hard schema was tried
                # and caused instability on very long outputs (Resources page):
                # the model got stuck escaping a multi-KB markdown string and
                # emitted corrupted JSON. ``_parse_llm_json`` handles minor
                # malformation; keep the nudge, skip the hard schema.
                kwargs = {"response_format": {"type": "json_object"}}
                content = prompt

            response = await dispatch_completion(
                provider=sniff_provider(self._model_name),
                model=normalize_litellm_model(self._model_name),
                messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens,
                temperature=temperature,
                # Issue #223: stream this (up to 32k-token) page compile so a long
                # generation never idles into the ~130s edge-proxy disconnect.
                stream=settings.wiki_llm_streaming,
                **kwargs,
            )
            return response.choices[0].message.content or "{}"  # type: ignore[index, union-attr]

    async def _call_llm(
        self,
        prompt: str,
        max_retries: int = 1,
        page_kind: str = "topic",
        validator: "Callable[[str], tuple[bool, str]] | None" = None,
    ) -> CompiledPageContent:
        from beever_atlas.infra.config import get_settings

        settings = get_settings()
        hardening = settings.wiki_parse_hardening
        # Phase 5: same branch as _llm_generate_json — delimited mode only for
        # markdown-content pages when wiki_compiler_v2=ON.
        use_delimited = settings.wiki_compiler_v2 and page_kind not in {"analysis", "translation"}

        data: dict = {}
        for attempt in range(1 + max_retries):
            raw = await self._llm_generate_json(
                prompt, temperature=0.2 + (attempt * 0.1), page_kind=page_kind
            )

            # 2g. Retry gating: skip retry for short or safety-blocked responses.
            if hardening and attempt == 0:
                raw_stripped = (raw or "").strip()
                if len(raw_stripped) < 100:
                    logger.warning(
                        "WikiCompiler: raw_too_short_no_retry, raw_len=%d", len(raw_stripped)
                    )
                    return CompiledPageContent(content="", summary="")
                if _is_safety_block(raw_stripped):
                    logger.warning(
                        "WikiCompiler: safety_block_no_retry, raw_head=%r", raw_stripped[:200]
                    )
                    return CompiledPageContent(content="", summary="")

            if use_delimited:
                parsed_page = _parse_delimited_response(raw or "")
                content = parsed_page.content.strip()
                summary = parsed_page.summary.strip()
                data = {"content": content, "summary": summary}

                # 1c. Degenerate-content guard (mirrors JSON branch).
                if hardening and content:
                    is_degen, reason = _is_degenerate_content(content)
                    if is_degen and attempt < max_retries:
                        logger.warning(
                            "WikiCompiler: degenerate content (%s, attempt %d), retrying with temperature=0.4",
                            reason,
                            attempt + 1,
                        )
                        raw2 = await self._llm_generate_json(
                            prompt
                            + "\n\nNOTE: The previous attempt produced degenerate output. Return real prose and real table rows.",
                            temperature=0.4,
                            page_kind=page_kind,
                        )
                        parsed2 = _parse_delimited_response(raw2 or "")
                        content2 = parsed2.content.strip()
                        summary2 = parsed2.summary.strip()
                        is_degen2, reason2 = (
                            _is_degenerate_content(content2) if content2 else (True, "empty")
                        )
                        if not is_degen2:
                            return CompiledPageContent(content=content2, summary=summary2)
                        logger.warning(
                            "WikiCompiler: degenerate content shipped for retry (%s)", reason2
                        )
                        return CompiledPageContent(
                            content=content2 or content, summary=summary2 or summary
                        )

                if content and validator is not None:
                    ok, reason = validator(content)
                    if not ok and attempt < max_retries:
                        logger.warning(
                            "WikiCompiler: validator_failed event=degraded_regeneration page_kind=%s attempt=%d reason=%s",
                            page_kind,
                            attempt + 1,
                            reason,
                        )
                        raw2 = await self._llm_generate_json(
                            prompt
                            + f"\n\nNOTE: The previous attempt failed validation — {reason} Fix this on retry.",
                            temperature=0.4,
                            page_kind=page_kind,
                        )
                        parsed2 = _parse_delimited_response(raw2 or "")
                        content2 = parsed2.content.strip()
                        summary2 = parsed2.summary.strip()
                        ok2, reason2 = validator(content2) if content2 else (False, "empty")
                        if ok2:
                            return CompiledPageContent(content=content2, summary=summary2)
                        logger.warning(
                            "WikiCompiler: validator_failed_twice event=deterministic_fallback page_kind=%s reason=%s",
                            page_kind,
                            reason2,
                        )
                        return CompiledPageContent(
                            content=content2 or content, summary=summary2 or summary
                        )
                if content:
                    return CompiledPageContent(content=content, summary=summary)
                if attempt < max_retries:
                    logger.info(
                        "WikiCompiler: empty content (attempt %d), retrying...", attempt + 1
                    )
                continue

            parsed = _parse_llm_json(raw)
            if parsed is None:
                dump_path = None
                if hardening and raw:
                    dump_path = _dump_parse_failure(raw)
                logger.warning(
                    "WikiCompiler: failed to parse LLM JSON (attempt %d), raw_len=%d. raw_head=%r dump=%s",
                    attempt + 1,
                    len(raw or ""),
                    (raw or "")[:200],
                    dump_path,
                )
                data = {}
                if attempt < max_retries:
                    logger.info(
                        "WikiCompiler: parse failure (attempt %d), retrying...", attempt + 1
                    )
                continue
            data = parsed if isinstance(parsed, dict) else {}
            content = data.get("content", "").strip()
            summary = data.get("summary", "").strip()

            # 1c. Degenerate-content guard: retry once with higher temperature.
            if hardening and content:
                is_degen, reason = _is_degenerate_content(content)
                if is_degen and attempt < max_retries:
                    logger.warning(
                        "WikiCompiler: degenerate content (%s, attempt %d), retrying with temperature=0.4",
                        reason,
                        attempt + 1,
                    )
                    # Force a retry with explicit degenerate note appended to prompt.
                    raw2 = await self._llm_generate_json(
                        prompt
                        + "\n\nNOTE: The previous attempt produced degenerate output. Return real prose and real table rows.",
                        temperature=0.4,
                        page_kind=page_kind,
                    )
                    parsed2 = _parse_llm_json(raw2)
                    if parsed2 and isinstance(parsed2, dict):
                        content2 = parsed2.get("content", "").strip()
                        summary2 = parsed2.get("summary", "").strip()
                        is_degen2, reason2 = (
                            _is_degenerate_content(content2) if content2 else (True, "empty")
                        )
                        if not is_degen2:
                            return CompiledPageContent(content=content2, summary=summary2)
                        logger.warning(
                            "WikiCompiler: degenerate content shipped for retry (%s)", reason2
                        )
                        return CompiledPageContent(
                            content=content2 or content, summary=summary2 or summary
                        )

            # Return immediately on any non-empty content
            if content:
                return CompiledPageContent(content=content, summary=summary)
            if attempt < max_retries:
                logger.info("WikiCompiler: empty content (attempt %d), retrying...", attempt + 1)
        logger.warning("WikiCompiler: empty content after %d attempts", 1 + max_retries)
        return CompiledPageContent(
            content=data.get("content", "").strip(),
            summary=data.get("summary", "").strip(),
        )

    async def _compile_overview(self, gathered: dict) -> WikiPage:
        summary = gathered["channel_summary"]
        clusters = gathered["clusters"]
        # Only topics that actually compiled to a page are valid wikilink
        # targets for the Overview's prose / cross-references. ``compile()``
        # stashes the list under ``_compiled_topic_titles``; fall back to
        # every cluster title when the key is absent (legacy/test callers).
        compiled_topic_titles: list[str] = gathered.get("_compiled_topic_titles") or [
            getattr(c, "title", "") or "" for c in clusters or []
        ]
        compiled_topic_titles = [t for t in compiled_topic_titles if t]
        # Use `memory_count` (not `member_count`) as the JSON key so the LLM
        # emits "N memories" instead of "N members" in Topics-at-a-Glance.
        clusters_data = [
            {
                "id": c.id,
                "title": c.title,
                "memory_count": c.member_count,
                "topic_tags": c.topic_tags,
            }
            for c in clusters
        ]
        # Build media data from media_facts
        media_data = _build_media_data(gathered["media_facts"])
        # Build graph entity data
        tech_data = [
            {"name": t["entity"].name, "used_by": t.get("used_by", [])}
            for t in gathered.get("technologies", [])
        ]
        project_data = [
            {
                "name": p["entity"].name,
                "deps": p.get("dependencies", []),
                "owners": p.get("owners", []),
            }
            for p in gathered.get("projects", [])
        ]

        # Aggregate key entities and relationships from all clusters
        all_key_entities: list[dict] = []
        all_key_relationships: list[dict] = []
        for c in clusters:
            all_key_entities.extend(c.key_entities[:5])
            all_key_relationships.extend(c.key_relationships[:5])

        # Glossary preview and FAQ count
        glossary_preview = summary.glossary_terms[:5] if summary.glossary_terms else []
        faq_count = sum(len(c.faq_candidates) for c in clusters)

        # Include skipped topics so overview can mention them briefly
        skipped_topics = gathered.get("_skipped_topics", [])
        if skipped_topics:
            for c_data in clusters_data:
                for st in skipped_topics:
                    if c_data.get("title") == st["title"]:
                        c_data["brief"] = True

        # Build a stable, indexed citation list that the LLM will reference by [N] number.
        # Using the same list for both the prompt and the WikiPage.citations ensures inline
        # citation numbers match what the UI renders in the Sources panel.
        # Cap to 12 facts (down from 20) — the LLM rarely uses all 20 and
        # unused indices become orphan citations ([6] defined, never referenced)
        # in the Sources list. Orphan strip in _postprocess_content is a
        # second line of defence but capping at the source reduces churn.
        citation_facts = (gathered["recent_facts"] + gathered["media_facts"])[:12]
        cited_facts_for_prompt = [
            {
                "index": i,
                "author": f.author_name,
                "excerpt": wrap_untrusted(f.memory_text[:120]),
                "timestamp": f.message_ts,
            }
            for i, f in enumerate(citation_facts, 1)
        ]

        # Always union top-level decisions with cluster-level decisions, then
        # dedup by (name, decided_by, date) tuple so the Overview "Decisions
        # Made" count reflects decisions scattered across clusters (previously
        # the cluster fallback only ran when the top-level list was empty,
        # hiding cluster decisions whenever ANY top-level decision existed).
        _top_level_decisions = gathered.get("decisions", []) or []
        _cluster_decisions = [
            d for c in gathered["clusters"] for d in getattr(c, "decisions", []) or []
        ]
        # Fallback: upstream consolidation may not populate `c.decisions` at
        # all, in which case the count silently stays 0 even though facts
        # typed as "decision" exist in `c.key_facts`. Harvest those too so
        # Key Highlights reflects the real decision volume.
        _fact_decisions = [
            {
                "name": kf.get("memory_text") or kf.get("fact") or "",
                "decided_by": kf.get("author_name") or "",
                "date": kf.get("message_ts") or "",
            }
            for c in gathered["clusters"]
            for kf in (getattr(c, "key_facts", []) or [])
            if isinstance(kf, dict)
            and (kf.get("fact_type") or kf.get("type") or "").lower() == "decision"
        ]
        _seen_keys: set[tuple] = set()
        gathered_decisions: list = []
        for d in list(_top_level_decisions) + list(_cluster_decisions) + list(_fact_decisions):
            if isinstance(d, dict):
                # Normalize name for dedup — truncate to first 60 chars so
                # the fact-harvested entries (which use full memory_text)
                # don't escape dedup against shorter structured decisions.
                raw_name = (d.get("name") or d.get("title") or "").strip().lower()
                key = (
                    raw_name[:60],
                    (d.get("decided_by") or "").strip().lower(),
                    (d.get("date") or "").strip()[:10],
                )
            else:
                key = (
                    str(getattr(d, "name", "") or getattr(d, "title", "")).strip().lower()[:60],
                    "",
                    "",
                )
            if key in _seen_keys:
                continue
            _seen_keys.add(key)
            gathered_decisions.append(d)

        prompt = self._fmt_prompt(
            OVERVIEW_PROMPT,
            channel_name=summary.channel_name,
            description=summary.description,
            text=summary.text,
            themes=summary.themes,
            momentum=summary.momentum,
            team_dynamics=summary.team_dynamics,
            decisions_count=len(gathered_decisions),
            people_count=len(summary.top_people),
            projects_count=len(summary.active_projects),
            tech_count=len(summary.tech_stack),
            media_count=summary.media_count,
            clusters_json=json.dumps(clusters_data, default=str),
            topic_graph_edges_json=json.dumps(summary.topic_graph_edges, default=str),
            recent_activity_json=json.dumps(summary.recent_activity_summary, default=str),
            top_people_json=json.dumps(summary.top_people, default=str),
            top_decisions_json=json.dumps(summary.top_decisions, default=str),
            technologies_json=json.dumps(tech_data, default=str),
            projects_json=json.dumps(project_data, default=str),
            key_entities_json=json.dumps(all_key_entities, default=str),
            key_relationships_json=json.dumps(all_key_relationships, default=str),
            media_json=json.dumps(media_data, default=str),
            glossary_preview_json=json.dumps(glossary_preview, default=str),
            faq_count=faq_count,
            cited_facts_json=json.dumps(cited_facts_for_prompt, default=str),
        )
        try:
            result = await self._call_llm(
                prompt,
                page_kind="overview",
                validator=combine(min_length(300), mermaid_balanced, banned_phrases),
            )
            content = result.content if result is not None else ""
            summary_text = result.summary if result is not None else ""
        except Exception as exc:
            logger.warning("WikiCompiler: overview page failed hard (%s); using fallback", exc)
            content, summary_text = "", ""
        stripped = (content or "").strip()
        if not stripped or _is_degenerate_content(content)[0]:
            logger.info(
                "WikiCompiler: overview content empty/degenerate, using deterministic fallback"
            )
            content, summary_text = _overview_fallback(summary, clusters)
        post_content = self._postprocess_content(content)
        post_content = self._strip_out_of_range_inline_citations(
            post_content, max_index=len(citation_facts)
        )
        post_content = _splice_overview_sections(
            post_content,
            channel_summary=summary,
            clusters=clusters,
            tech_data=tech_data,
            project_data=project_data,
            decisions_count=len(gathered_decisions),
            skipped_topics=skipped_topics,
        )
        # Section order matters: all three splicers append to the
        # body's tail, so the FIRST call lands closest to the top of
        # the appendix block. Order picked for reader-value priority:
        #   1. Core Discussions  — top decisions + key voices
        #   2. Project Status    — momentum + team dynamics
        #   3. Recent Updates    — week-over-week activity counts
        # ``_target_lang`` is normally set in ``__init__``; the
        # ``getattr`` fallback covers tests that construct the
        # compiler via ``__new__`` (e.g. ``test_wiki_compiler_empty_channel``)
        # without going through the regular constructor path.
        splicer_lang = getattr(self, "_target_lang", "en")
        post_content = _splice_core_discussions(
            post_content,
            top_decisions=getattr(summary, "top_decisions", []) or [],
            cited_facts=cited_facts_for_prompt,
            lang=splicer_lang,
        )
        post_content = _splice_project_status(
            post_content,
            momentum=getattr(summary, "momentum", "") or "",
            team_dynamics=getattr(summary, "team_dynamics", "") or "",
            lang=splicer_lang,
        )
        post_content = _splice_recent_updates(
            post_content,
            recent_activity_summary=getattr(summary, "recent_activity_summary", {}) or {},
            lang=splicer_lang,
        )
        # Final pass: rewrite any ``[[Title]]`` references into native
        # markdown links (compiled topics) or plain text (skipped /
        # unknown topics). Runs AFTER all splicers so deterministic content
        # added by the splicer is also covered.
        post_content = _rewrite_topic_wikilinks(post_content, compiled_topic_titles)
        return WikiPage(
            id="overview",
            slug="overview",
            title=self._page_title("overview"),
            page_type="fixed",
            section_number="1",
            content=post_content,
            summary=summary_text,
            memory_count=gathered["total_facts"],
            citations=self._filter_citations_to_body(
                post_content, _build_citations(citation_facts)
            ),
        )

    async def _analyze_topic(self, cluster, sorted_facts: list[AtomicFact]) -> dict | None:
        """Analyze a large topic cluster to decide if it needs sub-pages.

        Returns the parsed analysis dict or None if analysis fails or isn't needed.
        """
        indexed_facts = [
            {
                "index": i,
                "memory_text": wrap_untrusted(f.memory_text),
                "author_name": f.author_name,
                "fact_type": f.fact_type,
            }
            for i, f in enumerate(sorted_facts[:30])
        ]
        prompt = self._fmt_prompt(
            TOPIC_ANALYSIS_PROMPT,
            title=cluster.title,
            summary=cluster.summary,
            fact_count=len(sorted_facts),
            indexed_facts_json=json.dumps(indexed_facts, default=str),
        )
        for attempt in range(2):
            try:
                raw = await self._llm_generate_json(prompt, page_kind="analysis")
                data = json.loads(raw)
                if not isinstance(data, dict) or "needs_subpages" not in data:
                    logger.warning(
                        "WikiCompiler: topic analysis returned invalid structure for %s",
                        cluster.title,
                    )
                    return None
                return data
            except json.JSONDecodeError as exc:
                if attempt == 0:
                    logger.warning(
                        "WikiCompiler: topic analysis JSON truncated for %s "
                        "(attempt 1), retrying: %s",
                        cluster.title,
                        exc,
                    )
                    continue
                logger.warning(
                    "WikiCompiler: topic analysis failed for %s after retry: %s",
                    cluster.title,
                    exc,
                )
                return None
            except Exception as exc:  # noqa: BLE001
                logger.warning("WikiCompiler: topic analysis failed for %s: %s", cluster.title, exc)
                return None
        return None

    async def _compile_subtopic_page(
        self,
        parent_slug: str,
        parent_title: str,
        sub_info: dict,
        all_sorted_facts: list[AtomicFact],
        compiled_topic_titles: list[str] | None = None,
    ) -> WikiPage:
        """Compile a single sub-topic page from a subset of facts.

        ``compiled_topic_titles`` is the list of topic titles the threshold
        gate kept — wikilinks (including the ``[[Parent Title]]`` parent
        anchor) are constrained to that set so the page never emits a red
        broken link. ``None`` (legacy callers / tests) disables the
        constraint and treats every topic as valid.
        """
        # Coerce ``None`` to empty list — the rewrite helper treats both as
        # "no compiled set known" but downstream JSON serialisation needs a
        # concrete list.
        compiled_topic_titles_safe: list[str] = [t for t in (compiled_topic_titles or []) if t]
        fact_indices = sub_info.get("fact_indices", [])
        sub_facts = [all_sorted_facts[i] for i in fact_indices if i < len(all_sorted_facts)]
        facts_data = [
            {
                "memory_text": wrap_untrusted(f.memory_text),
                "author_name": f.author_name,
                "quality_score": f.quality_score,
                "fact_type": f.fact_type,
                "importance": f.importance,
                "message_ts": f.message_ts,
            }
            for f in sub_facts
        ]
        media_data = _build_media_data(sub_facts)
        sub_title = sub_info.get("title", "Untitled")
        sub_slug = _slugify(sub_title)

        fact_count = len(sub_facts)
        from beever_atlas.infra.config import get_settings

        v2 = get_settings().wiki_compiler_v2
        prompt_template = SUBTOPIC_PROMPT_V2 if v2 else SUBTOPIC_PROMPT
        prompt = self._fmt_prompt(
            prompt_template,
            parent_title=parent_title,
            title=sub_title,
            summary=sub_info.get("summary", ""),
            fact_count=fact_count,
            member_facts_json=json.dumps(facts_data, default=str),
            media_json=json.dumps(media_data, default=str),
            compiled_topic_titles_json=json.dumps(compiled_topic_titles_safe, default=str),
        )
        try:
            # Require Key Facts + Overview so thin sub-topic pages (TL;DR +
            # concept diagram only) trigger a retry instead of shipping.
            result = await self._call_llm(
                prompt,
                max_retries=2,
                page_kind="subtopic",
                validator=combine(
                    min_length(200),
                    mermaid_balanced,
                    required_headings(("Key Facts", "Overview")),
                ),
            )
            raw_content = result.content if result is not None else ""
            result_summary = result.summary if result is not None else ""
        except Exception as exc:
            logger.warning("WikiCompiler: subtopic page failed hard (%s); using fallback", exc)
            raw_content = ""
            result_summary = ""
        content = self._postprocess_content(raw_content)
        # Always splice Key Facts table + Overview deterministically when the
        # LLM drops them (this happens even after validator retries when the
        # model repeatedly skips sections). Uses sub_facts directly so the
        # table reflects this sub-topic's evidence, not the parent's.
        sub_key_facts = [
            {
                "memory_text": f.memory_text,
                "author_name": f.author_name,
                "fact_type": f.fact_type,
                "importance": f.importance,
                "quality_score": f.quality_score,
            }
            for f in sub_facts
        ]
        if v2:
            content = _splice_key_facts_table(content, sub_key_facts)
        content = _splice_subtopic_sections(
            content, sub_title, sub_facts, parent_title, sub_key_facts
        )
        stripped = (content or "").strip()
        if not stripped or len(stripped) < 50 or _is_degenerate_content(content)[0]:
            logger.info(
                "WikiCompiler: subtopic content empty/degenerate, using deterministic fallback"
            )
            fb_content, fb_summary = _subtopic_fallback(sub_title, sub_facts, parent_title)
            content = fb_content
            if not result_summary:
                result_summary = fb_summary
        # Rewrite ``[[Title]]`` references (including the ``[[Parent Title]]``
        # anchor the prompt asks for) into native links (compiled topics) or
        # plain text (skipped / unknown topics).
        content = _rewrite_topic_wikilinks(content, compiled_topic_titles_safe)
        page_id = f"topic-{parent_slug}--{sub_slug}"
        return WikiPage(
            id=page_id,
            slug=f"{parent_slug}--{sub_slug}",
            title=sub_title,
            page_type="sub-topic",
            parent_id=f"topic-{parent_slug}",
            content=content,
            summary=result_summary,
            memory_count=fact_count,
            size_tier=_compute_size_tier(fact_count),
            citations=self._filter_citations_to_body(content, _build_citations(sub_facts[:10])),
        )

    async def _compile_thin_topic(self, cluster, gathered: dict) -> WikiPage:
        """Phase 4 thin-topic path — TL;DR + table + 3-sentence summary only."""
        member_facts: list[AtomicFact] = gathered["cluster_facts"].get(cluster.id, [])
        sorted_facts = sorted(member_facts, key=lambda f: f.quality_score, reverse=True)
        # Defensive: thin-topic prompt does not currently emit ``[[Title]]``
        # wikilinks, but the LLM may invent them. Source the compiled-topic
        # set from ``gathered`` so the rewrite never emits broken links.
        compiled_topic_titles: list[str] = gathered.get("_compiled_topic_titles") or [
            getattr(c, "title", "") or "" for c in gathered.get("clusters", []) or []
        ]
        compiled_topic_titles = [t for t in compiled_topic_titles if t]
        facts_data = [
            {
                "memory_text": wrap_untrusted(f.memory_text),
                "author_name": f.author_name,
                "quality_score": f.quality_score,
                "fact_type": f.fact_type,
                "importance": f.importance,
                "message_ts": f.message_ts,
            }
            for f in sorted_facts
        ]
        slug = _slugify(cluster.title) or cluster.id
        prompt = self._fmt_prompt(
            THIN_TOPIC_PROMPT,
            title=cluster.title,
            summary=cluster.summary,
            fact_count=len(member_facts),
            key_facts_json=json.dumps(cluster.key_facts, default=str),
            member_facts_json=json.dumps(facts_data, default=str),
        )
        result = await self._call_llm(
            prompt,
            page_kind="topic",
            validator=combine(min_length(200), mermaid_balanced, required_headings(("Overview",))),
        )
        content = self._postprocess_content(result.content)
        # Marker substitution still runs (table may be empty -> "" replacement).
        content = _splice_key_facts_table(content, cluster.key_facts)
        if not content or len(content.strip()) < 50:
            content = _facts_fallback_content(sorted_facts)
        # Defensive rewrite — thin-topic prompt doesn't emit wikilinks but
        # protect against LLM drift.
        content = _rewrite_topic_wikilinks(content, compiled_topic_titles)
        return WikiPage(
            id=f"topic-{slug}",
            slug=slug,
            title=cluster.title,
            page_type="topic",
            content=content,
            summary=result.summary,
            memory_count=cluster.member_count,
            size_tier=_compute_size_tier(cluster.member_count),
            citations=self._filter_citations_to_body(content, _build_citations(sorted_facts[:20])),
        )

    async def _try_compile_topic_modular(
        self,
        cluster,
        gathered: dict,
        sorted_facts: list,
        sub_pages: list[WikiPage] | None = None,
    ) -> "WikiPage | None":
        """Run the adaptive-modules compiler for one topic.

        Returns a populated WikiPage on success, or None when the
        modular path produced unusable output (e.g., catastrophic
        fallback). The caller falls back to the legacy
        ``TOPIC_PROMPT`` flow when None is returned.

        This is a thin adapter — it builds the orchestrator's typed
        inputs from the cluster + gathered data, wraps the compiler's
        LLM in the orchestrator's expected callable shape, and maps
        the resulting ``ModularPageOutput`` to a ``WikiPage``.

        ``sub_pages`` (optional): when the caller has already split a
        large cluster into sub-topic pages via ``_analyze_topic`` +
        ``_compile_subtopic_page``, pass them here so the parent's
        ``subpage_cards`` module fires. The orchestrator's
        ``compute_signals`` reads ``child_count`` off the cluster dict
        and the ``subpage_cards`` predicate gates on ``child_count >= 1``.
        """
        from beever_atlas.wiki.modules.orchestrator import (
            compile_topic_page_modular,
        )
        from beever_atlas.wiki.modules.planner import compute_signals

        # Build per-module render_inputs from the gathered cluster data.
        # Missing keys (e.g., comparison alternatives, pros/cons,
        # quotes, process steps) silently produce empty modules — the
        # orchestrator logs a structured warning per missing key so
        # soak telemetry can spot the gap.
        facts_data = [
            {
                "memory_text": wrap_untrusted(f.memory_text),
                "author_name": f.author_name,
                "fact_type": f.fact_type,
                "importance": f.importance,
                "quality_score": f.quality_score,
                "message_ts": f.message_ts,
            }
            for f in sorted_facts[:30]
        ]
        decisions_data = list(getattr(cluster, "decisions", []) or [])
        # Entities + relationships from the cluster's knowledge-graph slice.
        entities_data = [
            {
                "id": str(e.get("name") or e.get("id") or ""),
                "label": str(e.get("name") or ""),
                "kind": str(e.get("type") or ""),
            }
            for e in (getattr(cluster, "key_entities", []) or [])
            if isinstance(e, dict)
        ]
        # Filter conversation-meta edges (MENTIONS, ASKS, REPLIED_TO,
        # etc.) before they reach the entity_diagram renderer. These
        # are message-level relationships generated by the extractor
        # for thread reconstruction; they overwhelm the topic's
        # actual concept graph (every "Thomas asks Jacky" turns into
        # an edge). Keep them in the underlying graph for other uses
        # but strip from the visual.
        _CONVERSATION_META_EDGES = {
            "MENTIONS",
            "MENTIONED",
            "ASKS",
            "ASKED",
            "REPLIED_TO",
            "REPLIES_TO",
            "ADDRESSES",
            "ADDRESSED",
            "INFORMS",
            "INFORMED",
            "EXPLAINS_TO",
            "EXPLAINED_TO",
        }
        relationships_data = [
            {
                "from": str(r.get("source") or r.get("from") or ""),
                "to": str(r.get("target") or r.get("to") or ""),
                "label": str(r.get("type") or r.get("label") or ""),
            }
            for r in (getattr(cluster, "key_relationships", []) or [])
            if isinstance(r, dict)
            and (str(r.get("type") or r.get("label") or "")).upper() not in _CONVERSATION_META_EDGES
        ]
        # ``cluster.open_questions`` is a SINGLE STRING (1-2 sentence
        # paragraph), NOT a list. Iterating it as a list would split it
        # into per-character bullets — that bug shipped once already.
        # Wrap into a single-question entry when non-empty.
        _oq_raw = getattr(cluster, "open_questions", "") or ""
        if isinstance(_oq_raw, list):
            open_questions_data = [
                {"question": q, "raised": ""} for q in _oq_raw if isinstance(q, str) and q.strip()
            ]
        elif isinstance(_oq_raw, str) and _oq_raw.strip():
            open_questions_data = [{"question": _oq_raw.strip(), "raised": ""}]
        else:
            open_questions_data = []
        # Related topics from the existing related_cluster_ids logic.
        # The actual ``shared_entities`` per cluster pair is already
        # computed by ``_compute_cross_cluster_shared_entities`` in the
        # consolidation service and persisted as
        # ``channel_summary.topic_graph_edges``. Look that up to surface
        # a real reason ("shared: jwt, oauth +2") instead of the
        # boilerplate "shared entities or contributors" that every
        # related entry used to repeat verbatim.
        all_clusters = gathered.get("clusters", [])
        _channel_summary = gathered.get("channel_summary")
        _edges_raw = getattr(_channel_summary, "topic_graph_edges", None) or []
        # Edges are stored undirected; key on the sorted (a, b) pair so
        # lookup works regardless of which side is the current cluster.
        edges_by_pair: dict[tuple[str, str], list[str]] = {}
        for edge in _edges_raw:
            if not isinstance(edge, dict):
                continue
            a_id = str(edge.get("source_cluster_id") or "")
            b_id = str(edge.get("target_cluster_id") or "")
            if not a_id or not b_id:
                continue
            shared = edge.get("shared_entities") or []
            if isinstance(shared, list):
                # ``sorted`` returns a list; cast to a fixed-arity
                # tuple so pyright infers the dict-key type correctly
                # and the lookup below stays type-stable.
                lo, hi = sorted([a_id, b_id])
                edges_by_pair[(lo, hi)] = [str(s) for s in shared if isinstance(s, str)]

        def _format_related_reason(shared_names: list[str]) -> str:
            cleaned = [n.strip() for n in shared_names if n and n.strip()]
            if not cleaned:
                return "shared entities or contributors"
            if len(cleaned) == 1:
                return f"shared: {cleaned[0]}"
            if len(cleaned) <= 3:
                return f"shared: {', '.join(cleaned)}"
            top = ", ".join(cleaned[:3])
            return f"shared: {top} +{len(cleaned) - 3} more"

        related_topics_data = []
        for rid in getattr(cluster, "related_cluster_ids", []):
            for rc in all_clusters:
                if rc.id == rid:
                    plo, phi = sorted([str(cluster.id), str(rc.id)])
                    shared = edges_by_pair.get((plo, phi), [])
                    related_topics_data.append(
                        {
                            "title": rc.title,
                            "slug": _slugify(rc.title) or rc.id,
                            "reason": _format_related_reason(shared),
                            "shared_entities": shared,
                            # Tier the score by how many entities overlap
                            # — gives the prompt a real signal to rank
                            # related links rather than a flat 0.5.
                            "score": min(0.4 + 0.1 * len(shared), 1.0) if shared else 0.5,
                        }
                    )
                    break

        # Glossary plumbing — surface the channel's glossary so the
        # ``acronym_legend`` module can filter to terms that ACTUALLY
        # appear on this page. Channel-level ``glossary_terms`` is a
        # list of strings; we coerce to dicts so the module's builder
        # has a uniform shape.
        channel_summary_obj = gathered.get("channel_summary")
        raw_glossary = getattr(channel_summary_obj, "glossary_terms", None) or []
        glossary_data: list[dict] = []
        for entry in raw_glossary:
            if isinstance(entry, dict):
                glossary_data.append(
                    {
                        "term": str(entry.get("term") or "").strip(),
                        "definition": str(entry.get("definition") or "").strip(),
                        "first_mentioned_by": str(
                            entry.get("first_mentioned_by") or entry.get("author") or ""
                        ).strip(),
                    }
                )
            elif isinstance(entry, str) and entry.strip():
                glossary_data.append(
                    {"term": entry.strip(), "definition": "", "first_mentioned_by": ""}
                )

        # Children payload for the ``subpage_cards`` module. When the
        # caller pre-split the cluster into sub-pages (≥15-fact path),
        # these become the parent's child cards; otherwise the list is
        # empty and the predicate (``child_count >= 1``) fails naturally.
        children_payload: list[dict] = []
        for sp in sub_pages or []:
            children_payload.append(
                {
                    "title": sp.title or "",
                    "slug": sp.slug or "",
                    "summary": (sp.summary or "")[:160],
                }
            )

        render_inputs = {
            "facts": [{**f, "memory_text": f["memory_text"]} for f in facts_data],
            "decisions": decisions_data,
            "entities": entities_data,
            "relationships": relationships_data,
            "open_questions": open_questions_data,
            "related_topics": related_topics_data,
            "glossary": glossary_data,
            "children": children_payload,
            # Other keys (events, alternatives, criteria, pros, cons,
            # quotes, process_steps, process_edges, media) are not yet
            # populated — modules requiring them will render empty
            # until the gather step is extended.
        }

        # Compute signals from the same data so the planner's
        # eligibility predicates and the validator agree.
        signals = compute_signals(
            cluster={
                "title": cluster.title,
                "member_facts": facts_data,
                "child_count": len(children_payload),
            },
            decisions=decisions_data,
            entities=entities_data,
            relationships=relationships_data,
            open_questions=open_questions_data,
            related_topics=related_topics_data,
            glossary=glossary_data,
        )

        # Top-N projections for the writer's prose context.
        top_facts = facts_data[:8]
        top_people = []
        seen_authors: set[str] = set()
        for f in facts_data:
            author = (f.get("author_name") or "").strip()
            if author and author not in seen_authors:
                top_people.append({"name": author, "role": ""})
                seen_authors.add(author)
                if len(top_people) >= 6:
                    break

        # Wrap the compiler's LLM in the orchestrator's expected
        # callable shape. The orchestrator handles parsing + retries.
        async def _llm_call(prompt: str) -> str:
            return await self._llm_generate_json(prompt, page_kind="topic")

        # Date range for the writer prompt context.
        date_start = ""
        date_end = ""
        try:
            date_start = str(getattr(cluster, "date_range_start", "") or "")
            date_end = str(getattr(cluster, "date_range_end", "") or "")
        except Exception:  # noqa: BLE001
            pass

        out = await compile_topic_page_modular(
            title=cluster.title,
            summary=getattr(cluster, "summary", "") or "",
            signals=signals,
            render_inputs=render_inputs,
            top_facts=top_facts,
            top_people=top_people,
            date_range_start=date_start,
            date_range_end=date_end,
            llm=_llm_call,
        )

        # If the orchestrator hit catastrophic fallback (LLM crash,
        # parse failure, total module rejection), don't ship the
        # half-rendered output — let the caller try the legacy path.
        if out.fell_back and not out.modules:
            return None

        # Telemetry — soak runs read these to compare cost/quality.
        logger.info(
            "modular_topic_page_compiled topic=%s modules=%d rendered=%d fell_back=%s",
            cluster.id,
            out.planner_module_count,
            out.rendered_module_count,
            out.fell_back,
        )

        slug = _slugify(cluster.title) or cluster.id
        # When sub-pages were pre-split (≥15-fact path), attach them as
        # WikiPageRef on the parent so the channel-tree builders can
        # walk the parent → children relationship without rerouting
        # through the legacy parent-page assembly.
        children_refs: list[WikiPageRef] = []
        for sp in sub_pages or []:
            children_refs.append(
                WikiPageRef(
                    id=sp.id,
                    title=sp.title,
                    slug=sp.slug,
                    section_number="",
                    memory_count=sp.memory_count,
                )
            )
        # Rewrite any ``[[Title]]`` references the modular path's narrative
        # may have produced into native markdown links (compiled topics) or
        # plain text (skipped / unknown topics).
        compiled_topic_titles_for_rewrite: list[str] = gathered.get("_compiled_topic_titles") or [
            getattr(c, "title", "") or "" for c in gathered.get("clusters", []) or []
        ]
        compiled_topic_titles_for_rewrite = [t for t in compiled_topic_titles_for_rewrite if t]
        modular_content = _rewrite_topic_wikilinks(out.content, compiled_topic_titles_for_rewrite)
        return WikiPage(
            id=f"topic-{slug}",
            slug=slug,
            title=cluster.title,
            page_type="topic",
            content=modular_content,
            summary=out.summary or getattr(cluster, "summary", "") or "",
            memory_count=cluster.member_count,
            size_tier=_compute_size_tier(cluster.member_count),
            citations=self._filter_citations_to_body(
                modular_content, _build_citations(sorted_facts[:20])
            ),
            modules=out.modules,
            # ``wiki-narrative-articles`` — propagate the validated
            # narrative payload through to the domain page so
            # ``model_dump`` writes it into the cached subdoc; the
            # persistence layer mirrors the field name and the read
            # path materialises it back. Empty list when the flag is
            # OFF or the validator rejected the LLM output.
            narrative_sections=list(getattr(out, "narrative_sections", []) or []),
            children=children_refs,
        )

    async def _compile_topic_page(self, cluster, gathered: dict) -> WikiPage | list[WikiPage]:
        """Compile a topic page. Returns a single page or [parent, *sub_pages] for large topics.

        Routing (post v1/v2 unification — see commit history for the
        original split):
          1. Modular path (``compile_topic_page_modular``) is the
             DEFAULT for ALL topic pages with at least 1 fact. The
             planner picks 3-7 modules from the catalog based on
             ``compute_signals`` — thin and rich pages alike share one
             prompt.
          2. The legacy ``TOPIC_PROMPT`` / ``THIN_TOPIC_PROMPT`` paths
             remain only as fallbacks for catastrophic modular
             failures (LLM crash, parse error, total module rejection).
             Sub-page split clusters (≥15 facts) still use the legacy
             ``TOPIC_PROMPT_V2`` for the parent overview today; subpage
             generation is its own pipeline.

        Returns a single page or ``[parent, *sub_pages]`` for large
        topics that needed splitting.
        """
        member_facts: list[AtomicFact] = gathered["cluster_facts"].get(cluster.id, [])
        # Only topics that actually compiled to a page are valid wikilink
        # targets for See Also / inline `[[Title]]` refs. ``compile()``
        # stashes the list under ``_compiled_topic_titles``; fall back to
        # every cluster title when the key is absent (legacy/test callers).
        compiled_topic_titles: list[str] = gathered.get("_compiled_topic_titles") or [
            getattr(c, "title", "") or "" for c in gathered.get("clusters", []) or []
        ]
        compiled_topic_titles = [t for t in compiled_topic_titles if t]
        from beever_atlas.infra.config import get_settings

        v2 = get_settings().wiki_compiler_v2
        sorted_facts = sorted(member_facts, key=lambda f: f.quality_score, reverse=True)
        slug = _slugify(cluster.title) or cluster.id

        # ── adaptive-wiki-page-content — modular path ────────────────
        # The module-aware single-call compiler is the DEFAULT for ALL
        # topic pages (small + mid + large) so the v2 cards render on
        # every topic regardless of size. The planner adapts the
        # module mix to the data density (3 modules for thin pages,
        # 5-7 for rich pages).
        #
        # ≥15-fact clusters first run ``_analyze_topic`` to (optionally)
        # split into sub-topic sub-pages. The sub-pages are produced
        # via the legacy ``_compile_subtopic_page`` flow (untouched —
        # sub-page rendering is its own pipeline). The PARENT page
        # rendering then routes through the modular path with the
        # sub-pages passed as ``children`` so the ``subpage_cards``
        # module fires on the parent's plan.
        sub_pages_for_parent: list[WikiPage] = []
        if member_facts and len(member_facts) >= TOPIC_SUBPAGE_THRESHOLD:
            analysis = await self._analyze_topic(cluster, sorted_facts)
            # Force a retry when the LLM says "no split" on a very large cluster
            # (≥40 facts). A 40+ row Key Facts table is unreadable, so treat
            # this as an analyzer error and ask again once.
            if (
                len(member_facts) >= 40
                and analysis is not None
                and not analysis.get("needs_subpages")
            ):
                logger.info(
                    "WikiCompiler: analyzer said no-split for large cluster '%s' "
                    "(%d facts); retrying once with stronger bias",
                    cluster.title,
                    len(member_facts),
                )
                analysis = await self._analyze_topic(cluster, sorted_facts)
            if (
                analysis
                and analysis.get("needs_subpages")
                and analysis.get("subpages")
                and len(sorted_facts) >= _TOPIC_SUBPAGE_MIN_FACTS
            ):
                try:
                    # Generate sub-pages in parallel — same as the legacy
                    # path. Sub-page rendering itself stays on the
                    # ``SUBTOPIC_PROMPT_V2`` flow; only the parent
                    # changes routing.
                    sub_coros = [
                        self._compile_subtopic_page(
                            slug,
                            cluster.title,
                            sub_info,
                            sorted_facts,
                            compiled_topic_titles=compiled_topic_titles,
                        )
                        for sub_info in analysis["subpages"]
                    ]
                    sub_results = await asyncio.gather(*sub_coros, return_exceptions=True)
                    raw_sub_pages: list[WikiPage] = []
                    for res in sub_results:
                        if isinstance(res, BaseException):
                            logger.warning(
                                "WikiCompiler: sub-page failed for topic %s: %s", cluster.title, res
                            )
                        else:
                            raw_sub_pages.append(res)

                    # Filter out empty/minimal sub-pages (< 50 chars of content).
                    for sp in raw_sub_pages:
                        if len(sp.content.strip()) >= 50:
                            sub_pages_for_parent.append(sp)
                        else:
                            logger.info(
                                "WikiCompiler: discarding empty sub-page '%s' for topic '%s'",
                                sp.title,
                                cluster.title,
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "WikiCompiler: sub-page generation failed for %s, "
                        "falling back to flat parent page: %s",
                        cluster.title,
                        exc,
                    )
                    sub_pages_for_parent = []

        # Always try the modular path FIRST (regardless of cluster
        # size). For ≥15-fact clusters, ``sub_pages_for_parent`` may
        # be non-empty and the planner's ``subpage_cards`` module
        # picks them up via ``signals.child_count``. For smaller
        # clusters, the list is empty and ``subpage_cards``'s
        # predicate (``child_count >= 1``) fails naturally.
        if member_facts:
            try:
                modular_page = await self._try_compile_topic_modular(
                    cluster,
                    gathered,
                    sorted_facts,
                    sub_pages=sub_pages_for_parent or None,
                )
                if modular_page is not None:
                    if sub_pages_for_parent:
                        return [modular_page, *sub_pages_for_parent]
                    return modular_page
            except Exception as exc:  # noqa: BLE001 — never block the page on modular failure
                logger.warning(
                    "modular_topic_compile_failed_falling_back_to_legacy "
                    "topic=%s exc_type=%s exc=%s",
                    cluster.id,
                    type(exc).__name__,
                    exc,
                )
            # Modular returned None (catastrophic fallback). Fall back to
            # the thin-topic legacy path for very small clusters so we
            # don't render an awkward 5-row table for a 2-fact page.
            # Only applies to clusters that didn't try the sub-page split.
            if v2 and not sub_pages_for_parent and len(member_facts) < _THIN_TOPIC_THRESHOLD:
                return await self._compile_thin_topic(cluster, gathered)
        facts_data = [
            {
                "memory_text": wrap_untrusted(f.memory_text),
                "author_name": f.author_name,
                "quality_score": f.quality_score,
                "fact_type": f.fact_type,
                "importance": f.importance,
                "message_ts": f.message_ts,
                "thread_context_summary": f.thread_context_summary,
            }
            for f in sorted_facts[:30]
        ]
        media_data = _build_media_data(member_facts)
        # ``slug`` is computed once at the top of this method now; do
        # not shadow.

        # Build related topics data for cross-references
        all_clusters = gathered["clusters"]
        related_topics = []
        for rid in getattr(cluster, "related_cluster_ids", []):
            for rc in all_clusters:
                if rc.id == rid:
                    related_topics.append(
                        {"id": f"topic-{_slugify(rc.title) or rc.id}", "title": rc.title}
                    )
                    break
        related_topics_json = json.dumps(related_topics, default=str)

        # Sub-page assembly for large clusters. When modular ran first
        # we already produced sub-pages (``sub_pages_for_parent``); the
        # legacy parent-prompt path reuses them rather than re-running
        # ``_analyze_topic`` + ``_compile_subtopic_page`` (would double
        # the LLM bill). When modular wasn't attempted (no member
        # facts) ``sub_pages_for_parent`` is empty and we fall through
        # to the flat parent path below.
        if len(member_facts) >= TOPIC_SUBPAGE_THRESHOLD:
            sub_pages: list[WikiPage] = list(sub_pages_for_parent)
            if sub_pages:
                try:
                    # Build parent overview page (without full detail — sub-pages have that)
                    parent_prompt = self._fmt_prompt(
                        TOPIC_PROMPT_V2 if v2 else TOPIC_PROMPT,
                        title=cluster.title,
                        summary=cluster.summary,
                        current_state=cluster.current_state,
                        open_questions=cluster.open_questions,
                        impact_note=cluster.impact_note,
                        topic_tags=", ".join(cluster.topic_tags),
                        date_range_start=cluster.date_range_start,
                        date_range_end=cluster.date_range_end,
                        authors=", ".join(cluster.authors),
                        fact_count=len(member_facts),
                        key_facts_json=json.dumps(cluster.key_facts, default=str),
                        decisions_json=json.dumps(cluster.decisions, default=str),
                        people_json=json.dumps(cluster.people, default=str),
                        technologies_json=json.dumps(cluster.technologies, default=str),
                        projects_json=json.dumps(cluster.projects, default=str),
                        key_entities_json=json.dumps(cluster.key_entities, default=str),
                        key_relationships_json=json.dumps(cluster.key_relationships, default=str),
                        member_facts_json=json.dumps(facts_data, default=str),
                        media_json=json.dumps(media_data, default=str),
                        related_topics_json=related_topics_json,
                        compiled_topic_titles_json=json.dumps(
                            list(compiled_topic_titles), default=str
                        ),
                    )
                    parent_result = await self._call_llm(parent_prompt, page_kind="topic")
                    parent_content = parent_result.content
                    if v2:
                        parent_content = self._postprocess_content(parent_content)
                        parent_content = _splice_key_facts_table(parent_content, cluster.key_facts)
                    # Rewrite any ``[[Title]]`` references the LLM emitted in
                    # See Also / inline prose into native links (compiled
                    # topics) or plain text (skipped / unknown topics).
                    parent_content = _rewrite_topic_wikilinks(parent_content, compiled_topic_titles)
                    children_refs = [
                        WikiPageRef(
                            id=sp.id,
                            title=sp.title,
                            slug=sp.slug,
                            section_number="",
                            memory_count=sp.memory_count,
                        )
                        for sp in sub_pages
                    ]
                    # ``parent_content`` already had the rewrite applied above
                    # for the v2 path; apply it to the non-v2 raw content too
                    # so both branches use compiled-topic-resolved wikilinks.
                    final_parent_content = (
                        parent_content
                        if v2
                        else _rewrite_topic_wikilinks(parent_result.content, compiled_topic_titles)
                    )
                    parent_page = WikiPage(
                        id=f"topic-{slug}",
                        slug=slug,
                        title=cluster.title,
                        page_type="topic",
                        content=final_parent_content,
                        summary=parent_result.summary,
                        memory_count=cluster.member_count,
                        size_tier=_compute_size_tier(cluster.member_count),
                        citations=self._filter_citations_to_body(
                            final_parent_content, _build_citations(sorted_facts[:20])
                        ),
                        children=children_refs,
                    )
                    return [parent_page, *sub_pages]
                except Exception as exc:
                    logger.warning(
                        "WikiCompiler: sub-page generation failed for %s, falling back to flat page: %s",
                        cluster.title,
                        exc,
                    )

        # Flat topic page (default path, or fallback from failed sub-page generation)
        prompt = self._fmt_prompt(
            TOPIC_PROMPT_V2 if v2 else TOPIC_PROMPT,
            title=cluster.title,
            summary=cluster.summary,
            current_state=cluster.current_state,
            open_questions=cluster.open_questions,
            impact_note=cluster.impact_note,
            topic_tags=", ".join(cluster.topic_tags),
            date_range_start=cluster.date_range_start,
            date_range_end=cluster.date_range_end,
            authors=", ".join(cluster.authors),
            fact_count=len(member_facts),
            key_facts_json=json.dumps(cluster.key_facts, default=str),
            decisions_json=json.dumps(cluster.decisions, default=str),
            people_json=json.dumps(cluster.people, default=str),
            technologies_json=json.dumps(cluster.technologies, default=str),
            projects_json=json.dumps(cluster.projects, default=str),
            key_entities_json=json.dumps(cluster.key_entities, default=str),
            key_relationships_json=json.dumps(cluster.key_relationships, default=str),
            member_facts_json=json.dumps(facts_data, default=str),
            media_json=json.dumps(media_data, default=str),
            related_topics_json=related_topics_json,
            compiled_topic_titles_json=json.dumps(list(compiled_topic_titles), default=str),
        )
        result = await self._call_llm(
            prompt,
            page_kind="topic",
            validator=combine(min_length(200), mermaid_balanced, required_headings(("Overview",))),
        )
        content = self._postprocess_content(result.content)
        if v2:
            content = _splice_key_facts_table(content, cluster.key_facts)
        if not content or len(content.strip()) < 50:
            content = _facts_fallback_content(sorted_facts)
        # Rewrite any ``[[Title]]`` references the LLM emitted in See Also /
        # inline prose into native links (compiled topics) or plain text
        # (skipped / unknown topics).
        content = _rewrite_topic_wikilinks(content, compiled_topic_titles)
        return WikiPage(
            id=f"topic-{slug}",
            slug=slug,
            title=cluster.title,
            page_type="topic",
            content=content,
            summary=result.summary,
            memory_count=cluster.member_count,
            size_tier=_compute_size_tier(cluster.member_count),
            citations=self._filter_citations_to_body(content, _build_citations(sorted_facts[:20])),
        )

    async def _compile_people(self, gathered: dict) -> WikiPage:
        channel_summary = gathered["channel_summary"]
        persons = gathered["persons"]
        # Only topics that actually compiled to a page are valid wikilink
        # targets for the People page. ``compile()`` stashes the list under
        # ``_compiled_topic_titles``; fall back to every cluster title when the
        # key is absent (legacy/test callers).
        compiled_topic_titles: list[str] = gathered.get("_compiled_topic_titles") or [
            getattr(c, "title", "") or "" for c in gathered.get("clusters", []) or []
        ]
        compiled_topic_titles = [t for t in compiled_topic_titles if t]
        try:
            relationship_edges = _format_relationship_edges(persons)
            prompt = self._fmt_prompt(
                PEOPLE_PROMPT,
                persons_json=json.dumps(persons, default=str),
                top_people_json=json.dumps(channel_summary.top_people, default=str),
                relationship_edges_json=json.dumps(relationship_edges, default=str),
                compiled_topic_titles_json=json.dumps(compiled_topic_titles, default=str),
            )
            result = await self._call_llm(prompt, page_kind="people")
            content = result.content if result is not None else ""
            summary_text = result.summary if result is not None else ""
        except Exception as exc:
            logger.warning("WikiCompiler: people page failed hard (%s); using fallback", exc)
            content, summary_text = "", ""
        stripped = (content or "").strip()
        if not stripped or _is_degenerate_content(content)[0]:
            logger.info(
                "WikiCompiler: people content empty/degenerate, using deterministic fallback"
            )
            content, summary_text = _people_fallback(persons, channel_summary.top_people or [])
        content = self._postprocess_content(content)
        # Post-process: rewrite any ``[[Title]]`` references into native
        # markdown links (compiled topics) or plain text (skipped topics).
        content = _rewrite_topic_wikilinks(content, compiled_topic_titles)
        return WikiPage(
            id="people",
            slug="people",
            title=self._page_title("people"),
            page_type="fixed",
            content=content,
            summary=summary_text,
            memory_count=len(persons),
        )

    async def _compile_decisions(self, gathered: dict) -> WikiPage:
        channel_summary = gathered["channel_summary"]
        # Only topics that actually compiled to a page are valid wikilink
        # targets in Decisions prose. ``compile()`` stashes the list under
        # ``_compiled_topic_titles``; fall back to every cluster title when
        # the key is absent (legacy/test callers).
        compiled_topic_titles: list[str] = gathered.get("_compiled_topic_titles") or [
            getattr(c, "title", "") or "" for c in gathered.get("clusters", []) or []
        ]
        compiled_topic_titles = [t for t in compiled_topic_titles if t]

        # Augment graph decisions with facts typed as 'decision' from cluster_facts.
        # This ensures Decisions page is non-empty even when entity_extractor misses
        # Decision-typed graph entities on chat channels.
        existing_decisions = list(gathered.get("decisions", []) or [])
        _seen_decision_names: set[str] = {
            (
                d.get("name") or d.get("title") or ""
                if isinstance(d, dict)
                else str(getattr(d, "name", "") or getattr(d, "title", ""))
            )
            .strip()
            .lower()[:60]
            for d in existing_decisions
        }
        for facts in gathered.get("cluster_facts", {}).values():
            for f in facts:
                ft = (getattr(f, "fact_type", "") or "").strip().lower()
                if ft != "decision":
                    continue
                text = (getattr(f, "memory_text", "") or getattr(f, "text", "") or "").strip()
                if not text:
                    continue
                key = text.lower()[:60]
                if key in _seen_decision_names:
                    continue
                _seen_decision_names.add(key)
                existing_decisions.append(
                    {
                        "name": text,
                        "decided_by": getattr(f, "author_name", "") or "",
                        "date": getattr(f, "message_ts", "") or "",
                        "fact_type": "decision",
                    }
                )

        prompt = self._fmt_prompt(
            DECISIONS_PROMPT,
            decisions_json=json.dumps(existing_decisions, default=str),
            top_decisions_json=json.dumps(channel_summary.top_decisions, default=str),
        )
        result = await self._call_llm(prompt, page_kind="decisions")
        content = self._postprocess_content(result.content)
        # Defensive: rewrite any ``[[Title]]`` references into native links
        # (compiled topics) or plain text (skipped / unknown topics).
        content = _rewrite_topic_wikilinks(content, compiled_topic_titles)
        return WikiPage(
            id="decisions",
            slug="decisions",
            title=self._page_title("decisions"),
            page_type="fixed",
            content=content,
            summary=result.summary,
            memory_count=len(existing_decisions),
        )

    async def _compile_faq(self, gathered: dict) -> WikiPage:
        """Compile FAQ page from aggregated faq_candidates across all TopicClusters."""
        clusters = gathered["clusters"]
        # Only topics that actually compiled to a page are valid wikilink
        # targets in FAQ "Related pages" / inline prose. ``compile()``
        # stashes the list under ``_compiled_topic_titles``; fall back to
        # every cluster title when the key is absent (legacy/test callers).
        compiled_topic_titles: list[str] = gathered.get("_compiled_topic_titles") or [
            getattr(c, "title", "") or "" for c in clusters or []
        ]
        compiled_topic_titles = [t for t in compiled_topic_titles if t]
        # Aggregate faq_candidates grouped by topic
        faq_by_topic: list[dict] = []
        topic_names: list[str] = []
        for cluster in clusters:
            if cluster.faq_candidates:
                faq_by_topic.append(
                    {
                        "topic": cluster.title,
                        "questions": cluster.faq_candidates,
                    }
                )
                topic_names.append(cluster.title)

        # Augment with facts typed as 'question' from cluster_facts.
        # This feeds the LLM more raw material so FAQ page renders even when
        # topic_summarizer produces no faq_candidates (common on chat channels).
        _seen_faq_questions: set[str] = {
            q.strip().lower()[:80]
            for entry in faq_by_topic
            for q in (entry.get("questions") or [])
            if isinstance(q, str)
        }
        cluster_by_id = {c.id: c for c in clusters}
        for cluster_id, facts in gathered.get("cluster_facts", {}).items():
            fact_questions: list[str] = []
            for f in facts:
                ft = (getattr(f, "fact_type", "") or "").strip().lower()
                if ft != "question":
                    continue
                text = (getattr(f, "memory_text", "") or getattr(f, "text", "") or "").strip()
                if not text:
                    continue
                key = text.lower()[:80]
                if key in _seen_faq_questions:
                    continue
                _seen_faq_questions.add(key)
                fact_questions.append(text)
            if fact_questions:
                cluster_obj = cluster_by_id.get(cluster_id)
                topic_label = cluster_obj.title if cluster_obj else cluster_id
                # Merge into existing entry for this topic or create a new one
                existing_entry = next((e for e in faq_by_topic if e["topic"] == topic_label), None)
                if existing_entry is not None:
                    existing_entry["questions"] = list(existing_entry["questions"]) + fact_questions
                else:
                    faq_by_topic.append({"topic": topic_label, "questions": fact_questions})
                    if topic_label not in topic_names:
                        topic_names.append(topic_label)

        prompt = self._fmt_prompt(
            FAQ_PROMPT,
            faq_candidates_json=json.dumps(faq_by_topic, default=str),
            topic_names_json=json.dumps(topic_names, default=str),
        )
        result = await self._call_llm(
            prompt,
            page_kind="faq",
            validator=combine(min_length(150), mermaid_balanced),
        )
        content = result.content
        summary = result.summary
        if not content.strip():
            logger.info("WikiCompiler: FAQ LLM returned empty, using deterministic fallback")
            content, summary = _faq_fallback(faq_by_topic, clusters)
        # Normalise heading-as-question / bare-bold-question forms into
        # the canonical ``**Q: ... ?**`` / ``A:`` block before persisting.
        # The frontend FaqPage parser tolerates several shapes, but
        # canonicalising here keeps the persisted markdown predictable
        # and downstream consumers (search, MCP read tools) see a
        # single shape regardless of LLM drift.
        content = _normalize_faq_content(content)
        content = self._postprocess_content(content)
        # Defensive: rewrite any ``[[Title]]`` references into native links
        # (compiled topics) or plain text (skipped / unknown topics).
        content = _rewrite_topic_wikilinks(content, compiled_topic_titles)
        return WikiPage(
            id="faq",
            slug="faq",
            title=self._page_title("faq"),
            page_type="fixed",
            content=content,
            summary=summary,
            memory_count=sum(len(c.faq_candidates) for c in clusters),
        )

    async def _compile_glossary(self, gathered: dict) -> WikiPage:
        """Compile Glossary page from ChannelSummary glossary_terms, enriched with graph entities."""
        channel_summary = gathered["channel_summary"]
        glossary_terms = list(channel_summary.glossary_terms or [])

        # Enrich with technology and project entity names
        existing = {t.lower() if isinstance(t, str) else str(t).lower() for t in glossary_terms}
        for tech in gathered.get("technologies", []):
            entity = tech.get("entity")
            name = entity.name if hasattr(entity, "name") else str(entity)
            if name.lower() not in existing:
                glossary_terms.append(name)
                existing.add(name.lower())
        for proj in gathered.get("projects", []):
            entity = proj.get("entity")
            name = entity.name if hasattr(entity, "name") else str(entity)
            if name.lower() not in existing:
                glossary_terms.append(name)
                existing.add(name.lower())

        # Add high-frequency entities (appearing in 3+ clusters)
        from collections import Counter

        entity_freq: Counter[str] = Counter()
        for cluster in gathered.get("clusters", []):
            for ent in cluster.key_entities:
                ename = ent.get("name", "") if isinstance(ent, dict) else str(ent)
                if ename:
                    entity_freq[ename] += 1
        for ename, count in entity_freq.items():
            if count >= 3 and ename.lower() not in existing:
                glossary_terms.append(ename)
                existing.add(ename.lower())

        # Filter out generic well-known terms
        glossary_terms = [
            t
            for t in glossary_terms
            if (t.lower() if isinstance(t, str) else str(t).lower()) not in GENERIC_GLOSSARY_TERMS
        ]

        # Cap at 30 terms
        glossary_terms = glossary_terms[:30]

        # Only topics that actually compiled to a page are valid wikilink
        # targets for the Glossary's Related-Topics column. ``compile()``
        # stashes the list under ``_compiled_topic_titles``; when the key
        # is absent (legacy/test callers), fall back to every cluster
        # title so the prompt + post-processor still have a target list.
        compiled_topic_titles: list[str] = gathered.get("_compiled_topic_titles") or [
            getattr(c, "title", "") or "" for c in gathered.get("clusters", []) or []
        ]
        compiled_topic_titles = [t for t in compiled_topic_titles if t]

        prompt = self._fmt_prompt(
            GLOSSARY_PROMPT,
            glossary_terms_json=json.dumps(glossary_terms, default=str),
            channel_description=channel_summary.description or channel_summary.channel_name,
            compiled_topic_titles_json=json.dumps(compiled_topic_titles, default=str),
        )
        result = await self._call_llm(
            prompt,
            page_kind="glossary",
            validator=combine(min_length(200), mermaid_balanced),
        )
        content = result.content
        summary = result.summary
        if not content.strip():
            logger.info("WikiCompiler: Glossary LLM returned empty, using deterministic fallback")
            content, summary = _glossary_fallback(glossary_terms, gathered.get("clusters", []))
        # Post-process BEFORE splicing: _postprocess_content auto-closes any
        # unclosed ```mermaid fence. If we splice first, the auto-closer runs
        # afterwards and seals the close fence AFTER our appended content —
        # which drags Introduction + Terms INTO the mermaid block and
        # renders as a mermaid syntax error.
        content = self._postprocess_content(content)
        content = _scrub_glossary_placeholders(content)
        content = _splice_glossary_sections(
            content,
            glossary_terms,
            gathered.get("clusters", []) or [],
            compiled_topic_titles=compiled_topic_titles,
        )
        # Final pass: rewrite any leftover ``[[Title]]`` references into
        # native markdown links (compiled topics) or plain text (skipped /
        # unknown topics). Runs AFTER the splice so deterministic-fallback
        # rows added by the splicer are also covered.
        content = _rewrite_topic_wikilinks(content, compiled_topic_titles)
        return WikiPage(
            id="glossary",
            slug="glossary",
            title=self._page_title("glossary"),
            page_type="fixed",
            content=content,
            summary=summary,
            memory_count=len(glossary_terms),
        )

    async def _compile_resources(self, gathered: dict) -> WikiPage:
        """Compile Resources & Media page deterministically from media_facts.

        Replaced the previous LLM call (which emitted large escape-heavy JSON
        that overflowed max_output_tokens and produced truncated/unparseable
        output) with pure Python markdown assembly.  The markdown structure and
        section headings are identical to those the old RESOURCES_PROMPT asked
        the LLM to produce, so frontend rendering is unchanged.
        """
        media_facts = gathered.get("media_facts", [])
        try:
            media_data = _build_media_data(media_facts)
            media_data = self._filter_media_for_resources(media_data)
            content = _assemble_resources_markdown(media_data)
            summary = (
                f"Catalog of {len(media_data)} shared resource(s) across "
                f"{len({item['type'] for item in media_data})} type(s)."
                if media_data
                else "No shared resources found."
            )
        except Exception as exc:
            logger.warning("WikiCompiler: resources page failed hard (%s); using fallback", exc)
            media_data = []
            content, summary = "", ""
        if not (content or "").strip():
            logger.info("WikiCompiler: resources content empty, using deterministic fallback")
            content, summary = _resources_fallback(media_data)

        return WikiPage(
            id="resources",
            slug="resources",
            title=self._page_title("resources"),
            page_type="fixed",
            content=content,
            summary=summary,
            memory_count=len(media_data),
            citations=self._filter_citations_to_body(content, _build_citations(media_facts[:20])),
        )

    async def _compile_activity(self, gathered: dict) -> WikiPage:
        channel_summary = gathered["channel_summary"]
        # Only topics that actually compiled to a page are valid wikilink
        # targets in Activity prose. ``compile()`` stashes the list under
        # ``_compiled_topic_titles``; fall back to every cluster title when
        # the key is absent (legacy/test callers).
        compiled_topic_titles: list[str] = gathered.get("_compiled_topic_titles") or [
            getattr(c, "title", "") or "" for c in gathered.get("clusters", []) or []
        ]
        compiled_topic_titles = [t for t in compiled_topic_titles if t]
        recent_data = [
            {
                "memory_text": wrap_untrusted(f.memory_text),
                "author_name": f.author_name,
                "message_ts": f.message_ts,
                "fact_type": f.fact_type,
                "source_media_type": f.source_media_type,
            }
            for f in gathered["recent_facts"]
        ]
        # Include recent media
        recent_media = _build_media_data(gathered["recent_facts"])

        try:
            prompt = self._fmt_prompt(
                ACTIVITY_PROMPT,
                recent_facts_json=json.dumps(recent_data, default=str),
                recent_activity_json=json.dumps(
                    channel_summary.recent_activity_summary, default=str
                ),
                recent_media_json=json.dumps(recent_media, default=str),
            )
            result = await self._call_llm(prompt, page_kind="activity")
            content = result.content if result is not None else ""
            summary_text = result.summary if result is not None else ""
        except Exception as exc:
            logger.warning("WikiCompiler: activity page failed hard (%s); using fallback", exc)
            content, summary_text = "", ""
        stripped = (content or "").strip()
        if not stripped or _is_degenerate_content(content)[0]:
            logger.info(
                "WikiCompiler: activity content empty/degenerate, using deterministic fallback"
            )
            content, summary_text = _activity_fallback(
                gathered["recent_facts"],
                channel_summary.recent_activity_summary or {},
                gathered.get("clusters", []),
            )
        content = self._postprocess_content(content)
        # Defensive: rewrite any ``[[Title]]`` references into native links
        # (compiled topics) or plain text (skipped / unknown topics).
        content = _rewrite_topic_wikilinks(content, compiled_topic_titles)
        return WikiPage(
            id="activity",
            slug="activity",
            title=self._page_title("activity"),
            page_type="fixed",
            content=content,
            summary=summary_text,
            memory_count=len(gathered["recent_facts"]),
        )

    async def _translate_cluster_titles(self, clusters: list) -> dict[str, str]:
        """Translate topic-cluster titles from source_lang into target_lang.

        Topic cluster titles are baked at consolidation time in the source
        language. When the wiki renders in a different target language, we
        translate titles once per compile in a single batched LLM call so
        the sidebar, topic cards, and page headers all read natively.

        Returns a dict ``{cluster_id: translated_title}``. Clusters missing
        from the dict keep their original title. An empty dict is returned
        when source_lang == target_lang, when there are no clusters, or
        when the LLM call fails (caller falls back to the original).

        Proper nouns (people, product, tool names) must stay as-is — the
        prompt repeats the Language Directive rule for that.
        """
        if self._target_lang == self._source_lang or not clusters:
            return {}
        pairs = [{"id": c.id, "title": c.title} for c in clusters if c.title]
        if not pairs:
            return {}

        # Build ad-hoc (not via _fmt_prompt) because the prompt explicitly names
        # both languages — the usual page-body language header isn't relevant
        # here and would only add token overhead.
        pairs_json = json.dumps(pairs, ensure_ascii=False)
        prompt = (
            f"Translate these topic titles from {self._source_lang} (BCP-47) "
            f"to {self._target_lang} (BCP-47).\n\n"
            "Rules:\n"
            "- Preserve proper nouns VERBATIM (people names, tool names, "
            "product names, company names, project codenames).\n"
            "- Native-script proper nouns stay in their native script; "
            "romanized names stay romanized.\n"
            "- Keep titles concise — do not expand or editorialize.\n"
            "- Return JSON only, no prose, no markdown fences.\n\n"
            f"Input (list of {{id, title}}):\n{pairs_json}\n\n"
            "Output JSON shape:\n"
            '{"titles": [{"id": "<cluster_id>", "title": "<translated>"}]}'
        )
        try:
            raw = await self._llm_generate_json(prompt, temperature=0.2, page_kind="translation")
            parsed = _parse_llm_json(raw)
            if not isinstance(parsed, dict):
                return {}
            items = parsed.get("titles") or []
            # Originals map lets us (a) reject hallucinated ids and (b) detect
            # proper-noun drift for identifier-shaped titles. Plain single-word
            # nouns like "Meeting" must still translate, so the handle gate
            # looks for identifier-shape signals (mention prefixes, separators,
            # digits, internal capitals) rather than just "single ASCII token".
            originals = {p["id"]: p["title"] for p in pairs}
            out: dict[str, str] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                cid = item.get("id")
                title = item.get("title")
                if not (isinstance(cid, str) and isinstance(title, str) and title.strip()):
                    continue
                if cid not in originals:
                    logger.warning(
                        "WikiCompiler: unknown cluster id %s in translation response",
                        cid,
                    )
                    continue
                if cid in out:
                    logger.warning(
                        "WikiCompiler: duplicate cluster id %s in translation response",
                        cid,
                    )
                    continue
                stripped = title.strip()
                original = originals[cid]
                if _looks_like_handle(original) and stripped != original:
                    logger.warning(
                        "WikiCompiler: rejecting suspicious title translation for handle-like id %s (%r -> %r)",
                        cid,
                        original,
                        stripped,
                    )
                    continue
                # Length sanity: reject pathological expansions (>3x char length).
                if len(stripped) > max(40, 3 * len(original)):
                    logger.warning(
                        "WikiCompiler: rejecting over-long title translation for %s (%d -> %d chars)",
                        cid,
                        len(original),
                        len(stripped),
                    )
                    continue
                out[cid] = stripped
            logger.info(
                "WikiCompiler: translated %d/%d topic titles to %s",
                len(out),
                len(pairs),
                self._target_lang,
            )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "WikiCompiler: topic-title translation failed (%s), keeping originals",
                exc,
            )
            return {}

    async def compile(
        self,
        gathered: dict,
        on_page_compiled: Any | None = None,
    ) -> dict[str, WikiPage]:
        """Compile all pages from gathered data in parallel.

        Args:
            gathered: Data from WikiDataGatherer.
            on_page_compiled: Optional async callback(page_id, pages_done, pages_completed)
                called each time a page finishes compilation.
        """
        from beever_atlas.infra.config import get_settings

        parallel_dispatch = get_settings().wiki_parallel_dispatch

        clusters = gathered["clusters"]
        # Defend against empty cluster titles reaching any LLM prompt/render path.
        _apply_title_fallbacks(clusters)
        channel_summary = gathered["channel_summary"]

        pages: dict[str, WikiPage] = {}
        pages_completed: list[str] = []

        async def _tracked(coro, page_key: str):
            """Wrap a compile coroutine to track completion."""
            result = await coro
            pages_completed.append(page_key)
            if on_page_compiled:
                await on_page_compiled(page_key, len(pages_completed), list(pages_completed))
            return result

        if parallel_dispatch:
            # 1d. Dispatch title translation as a background task so it runs
            # concurrently with pages that do not need translated titles
            # (people, decisions, activity, resources).
            title_map_task: asyncio.Task = asyncio.create_task(
                self._translate_cluster_titles(clusters)
            )

            async def _apply_titles_to_gathered() -> dict:
                """Await translation and return a gathered dict with updated titles."""
                title_map = await title_map_task
                if not title_map:
                    return gathered
                updated_clusters = [
                    c.model_copy(update={"title": title_map[c.id]}) if c.id in title_map else c
                    for c in clusters
                ]
                return {**gathered, "clusters": updated_clusters}

        else:
            # Serial fallback: await translation before dispatching any page.
            title_map = await self._translate_cluster_titles(clusters)
            if title_map:
                clusters = [
                    c.model_copy(update={"title": title_map[c.id]}) if c.id in title_map else c
                    for c in clusters
                ]
                gathered = {**gathered, "clusters": clusters}

        # Resolve which clusters will actually compile to a topic page BEFORE
        # dispatching any LLM call. The Glossary needs to know the compiled set
        # so its Related-Topics column never points at a topic the threshold
        # gate skipped — pre-computing here means the dispatch path doesn't
        # have to wait on topic-compile to finish to surface that list.
        channel_themes = channel_summary.themes if hasattr(channel_summary, "themes") else []
        if isinstance(channel_themes, str):
            channel_themes = [channel_themes]
        filtered_clusters: list = []
        skipped_topics: list[dict] = []
        total_cluster_count = len(clusters)
        compile_min_facts = _resolve_topic_compile_threshold(total_cluster_count)
        for c in clusters:
            should_include, skip_reason = self._is_topic_relevant(
                c,
                channel_themes,
                gathered["cluster_facts"],
                total_cluster_count,
                min_facts_override=compile_min_facts,
            )
            if should_include:
                filtered_clusters.append(c)
            else:
                logger.info("WikiCompiler: skipping topic '%s' (%s)", c.title, skip_reason)
                skipped_topics.append(
                    {"title": c.title, "reason": skip_reason, "member_count": c.member_count}
                )

        # Stash both lists on ``gathered`` so per-page compile coroutines can
        # read them. The Glossary uses ``_compiled_topic_titles`` to filter
        # its Related-Topics wikilinks; the Overview already consumes
        # ``_skipped_topics``.
        compiled_topic_titles = [getattr(c, "title", "") or "" for c in filtered_clusters]
        gathered["_skipped_topics"] = skipped_topics
        gathered["_compiled_topic_titles"] = compiled_topic_titles

        # Build list of (key, coroutine) pairs, gating conditional pages BEFORE dispatching LLM calls
        fixed_tasks: list[tuple[str, Any]] = []

        if parallel_dispatch:
            # Pages that need translated cluster titles await the task internally.
            async def _overview_with_titles():
                return await self._compile_overview(await _apply_titles_to_gathered())

            fixed_tasks.append(("overview", _tracked(_overview_with_titles(), "overview")))
            # Pages that do NOT reference cluster titles run immediately in parallel with translation.
            fixed_tasks.append(("people", _tracked(self._compile_people(gathered), "people")))
        else:
            fixed_tasks.append(("overview", _tracked(self._compile_overview(gathered), "overview")))
            fixed_tasks.append(("people", _tracked(self._compile_people(gathered), "people")))

        # Conditional: decisions — skip if 0 decisions
        if len(gathered.get("decisions", [])) > 0:
            fixed_tasks.append(
                ("decisions", _tracked(self._compile_decisions(gathered), "decisions"))
            )
        else:
            logger.info("WikiCompiler: skipping Decisions page (0 decisions)")

        # Conditional: FAQ — skip if 0 faq_candidates across all clusters
        total_faq = sum(len(c.faq_candidates) for c in clusters)
        if total_faq > 0:
            if parallel_dispatch:

                async def _faq_with_titles():
                    return await self._compile_faq(await _apply_titles_to_gathered())

                fixed_tasks.append(("faq", _tracked(_faq_with_titles(), "faq")))
            else:
                fixed_tasks.append(("faq", _tracked(self._compile_faq(gathered), "faq")))
        else:
            logger.info("WikiCompiler: skipping FAQ page (0 faq candidates)")

        # Conditional: glossary — skip if 0 glossary_terms
        if len(channel_summary.glossary_terms or []) > 0:
            if parallel_dispatch:

                async def _glossary_with_titles():
                    return await self._compile_glossary(await _apply_titles_to_gathered())

                fixed_tasks.append(("glossary", _tracked(_glossary_with_titles(), "glossary")))
            else:
                fixed_tasks.append(
                    ("glossary", _tracked(self._compile_glossary(gathered), "glossary"))
                )
        else:
            logger.info("WikiCompiler: skipping Glossary page (0 glossary terms)")

        # Always generate: activity (no cluster title references)
        fixed_tasks.append(("activity", _tracked(self._compile_activity(gathered), "activity")))

        # Conditional: resources — skip if 0 media (no cluster title references)
        media_data = _build_media_data(gathered.get("media_facts", []))
        if len(media_data) > 0:
            fixed_tasks.append(
                ("resources", _tracked(self._compile_resources(gathered), "resources"))
            )
        else:
            logger.info("WikiCompiler: skipping Resources page (0 media)")

        # Topic dispatch — ``filtered_clusters`` was computed above so the
        # Glossary task can read ``_compiled_topic_titles`` from ``gathered``
        # while we set up the per-topic coroutines.
        topic_parallelism = get_settings().wiki_topic_compile_parallelism
        topic_sem = asyncio.Semaphore(topic_parallelism)
        logger.info(
            "WikiCompiler: topic_compile_parallelism=%d topics=%d",
            topic_parallelism,
            len(filtered_clusters),
        )

        async def _bounded_topic(coro):
            async with topic_sem:
                return await coro

        if parallel_dispatch:

            async def _compile_topic_with_titles(cluster):
                updated_gathered = await _apply_titles_to_gathered()
                updated_gathered["_skipped_topics"] = skipped_topics
                return await self._compile_topic_page(cluster, updated_gathered)

            topic_tasks = [
                (
                    f"topic-{_slugify(c.title) or c.id}",
                    _tracked(
                        _bounded_topic(_compile_topic_with_titles(c)),
                        f"topic-{_slugify(c.title) or c.id}",
                    ),
                )
                for c in filtered_clusters
            ]
        else:
            topic_tasks = [
                (
                    f"topic-{_slugify(c.title) or c.id}",
                    _tracked(
                        _bounded_topic(self._compile_topic_page(c, gathered)),
                        f"topic-{_slugify(c.title) or c.id}",
                    ),
                )
                for c in filtered_clusters
            ]

        all_keys = [k for k, _ in fixed_tasks] + [k for k, _ in topic_tasks]
        all_coros = [c for _, c in fixed_tasks] + [c for _, c in topic_tasks]

        results = await asyncio.gather(*all_coros, return_exceptions=True)

        for key, res in zip(all_keys, results):
            if isinstance(res, BaseException):
                logger.error("WikiCompiler: failed to compile %s: %s", key, res, exc_info=res)
            elif isinstance(res, list):
                # Sub-page result: [parent_page, *sub_pages]
                for page in res:
                    pages[page.id] = page
            else:
                page: WikiPage = res
                pages[page.id] = page

        return pages

    # ------------------------------------------------------------------
    # llm-wiki-folder-structure — folder index synthesis + tree shaping
    # ------------------------------------------------------------------

    @staticmethod
    def apply_folder_plan_to_structure(
        structure: WikiStructure,
        *,
        plan: Any,
        folder_pages: dict[str, WikiPage],
    ) -> WikiStructure:
        """Rearrange a built WikiStructure to honour a folder plan.

        ``structure`` is the output of ``build_structure`` (today's
        flat layout — topics + fixed pages at root, sub-topics
        nested). ``plan`` is the planner's output (PlannedStructure-
        like). ``folder_pages`` is the dict produced by
        ``compile_folders``.

        Produces a NEW WikiStructure where:
          - Each planned folder becomes a root-level WikiPageNode (with
            its own page_type="folder").
          - Each leaf topic node mentioned in a folder's child_slugs
            is moved INTO that folder's children, in plan order.
          - Topics not assigned to any folder, plus all fixed pages,
            remain at root.

        When ``plan`` has no folders OR ``folder_pages`` is empty, the
        original structure is returned unchanged — the function is a
        safe no-op when planning is OFF.

        Section numbers are recomputed by ``assign_section_numbers``
        AFTER the rearrangement so paths reflect the new tree.
        """
        from beever_atlas.wiki.section_numbering import assign_section_numbers

        folders = list(getattr(plan, "folders", None) or []) if plan else []
        if not folders or not folder_pages:
            return structure

        # Index existing root nodes by slug → node so we can pluck them
        # into folders.
        root_nodes_by_slug: dict[str, WikiPageNode] = {}
        # Preserve original order for nodes that stay at root.
        original_order: list[WikiPageNode] = list(structure.pages)
        for n in original_order:
            if n.slug:
                root_nodes_by_slug[n.slug] = n

        # Map planned folder slug → set of child cluster slugs.
        folders_by_slug: dict[str, list[str]] = {}
        folder_titles: dict[str, str] = {}
        for f in folders:
            f_slug = getattr(f, "slug", None) or ""
            if not f_slug:
                continue
            folders_by_slug[f_slug] = list(getattr(f, "child_slugs", None) or [])
            folder_titles[f_slug] = getattr(f, "title", None) or f_slug

        # Build ALL folder nodes first (without children), then wire
        # children in a second pass — this lets folders reference each
        # other (nested folders). Order:
        #   1. Materialize each folder as an empty WikiPageNode.
        #   2. For each folder, populate its children from root_nodes_by_slug
        #      OR from sibling folder nodes (built in step 1).
        #   3. Mark which folders are referenced as children of other
        #      folders — those are NOT root nodes; only orphan folders
        #      (top-level) appear at root.
        folder_nodes_by_slug: dict[str, WikiPageNode] = {}
        for f_slug in folders_by_slug:
            f_page = folder_pages.get(f"folder-{f_slug}")
            if f_page is None:
                # No synthesized folder page — skip; its planned children
                # will fall through to root in the final assembly below.
                continue
            folder_nodes_by_slug[f_slug] = WikiPageNode(
                id=f_page.id,
                title=f_page.title,
                slug=f_page.slug,
                section_number="",  # recomputed at end
                page_type="folder",
                memory_count=f_page.memory_count,
                children=[],  # filled in pass 2
                is_synthetic=True,
            )

        # Pass 2: wire children. A folder's child slug may reference
        # either a leaf topic (in root_nodes_by_slug) OR another folder
        # (in folder_nodes_by_slug). Track which slugs end up nested
        # so we can exclude them from the root-level list.
        consumed_slugs: set[str] = set()
        nested_folder_slugs: set[str] = set()
        for f_slug, child_slugs in folders_by_slug.items():
            folder_node = folder_nodes_by_slug.get(f_slug)
            if folder_node is None:
                continue
            for cs in child_slugs:
                # Prefer topic node; fall back to sibling folder node.
                child_node = root_nodes_by_slug.get(cs)
                if child_node is not None:
                    folder_node.children.append(child_node)
                    consumed_slugs.add(cs)
                    continue
                child_folder = folder_nodes_by_slug.get(cs)
                if child_folder is not None:
                    folder_node.children.append(child_folder)
                    nested_folder_slugs.add(cs)

        # Drop folders that ended up with zero children (planner
        # references all unresolved or no-op).
        for f_slug in list(folder_nodes_by_slug.keys()):
            if not folder_nodes_by_slug[f_slug].children:
                folder_nodes_by_slug.pop(f_slug)
                # If it was nested, removing it doesn't matter; if it was
                # at root it just doesn't appear. Either way safe.

        # Root folders are those NOT nested inside another folder.
        root_folders = [
            folder_nodes_by_slug[slug]
            for slug in folders_by_slug
            if slug in folder_nodes_by_slug and slug not in nested_folder_slugs
        ]

        # Build the new root order: root folders first (in plan order),
        # then the original root nodes not consumed by any folder
        # (preserves fixed pages and unassigned topics).
        new_pages: list[WikiPageNode] = list(root_folders) + [
            n for n in original_order if n.slug not in consumed_slugs
        ]

        # Reset and recompute section numbers across the new tree so
        # every node's path reflects its actual position.
        def _reset(nodes: list[WikiPageNode]) -> None:
            for n in nodes:
                n.section_number = ""
                _reset(n.children)

        _reset(new_pages)
        assign_section_numbers(new_pages)

        return WikiStructure(
            channel_id=structure.channel_id,
            channel_name=structure.channel_name,
            platform=structure.platform,
            generated_at=structure.generated_at,
            is_stale=structure.is_stale,
            pages=new_pages,
        )

    async def _compile_folder_page(
        self,
        *,
        folder_slug: str,
        folder_title: str,
        children_pages: list[WikiPage],
        compiled_topic_titles: list[str] | None = None,
    ) -> WikiPage:
        """Synthesize a folder index page from its already-compiled children.

        Routing:
          1. Modular path (``compile_folder_page_modular``) is the
             DEFAULT. The planner picks 5-7 dashboard modules from the
             folder-archetype catalog (hero_summary, subpage_cards,
             folder_stats, top_contributors, cross_cutting_decisions,
             open_questions, provenance_drawer) — replacing the legacy
             "Themes & threads" prose with at-a-glance modules.
          2. Legacy ``FOLDER_INDEX_PROMPT`` path remains as the
             fallback for catastrophic modular failures (LLM crash,
             parse error, plan validates to empty). The legacy path
             still substitutes ``<<CHILDREN_TOC>>`` so the markdown
             render lane keeps working.

        Returns a ``WikiPage`` with ``page_type="folder"``,
        ``children_fingerprint`` set to a stable SHA-256 of sorted child
        slugs, and ``is_synthetic=True``.

        ``children_pages`` MUST be the leaves (or sub-folders) the
        planner placed in this folder. Order is preserved on output.
        """
        from beever_atlas.wiki.modules.orchestrator import (
            compile_folder_page_modular,
        )
        from beever_atlas.wiki.prompts import build_folder_index_prompt
        from beever_atlas.wiki.render import apply_children_toc_marker
        import hashlib

        # ── Modular folder dashboard path (default) ────────────────────
        # Build the descendant aggregate from each child's citations.
        # Citations carry the (author, text_excerpt) shape we need for
        # contributor + decision aggregation. This is best-effort: when
        # a child page lacks structured fact_type metadata (legacy
        # pages, thin topics), the citation's text still feeds
        # ``folder_stats.memories`` and ``top_contributors`` even
        # though it cannot reach the decision/question buckets.
        descendants_payload: list[dict[str, Any]] = []
        for c in children_pages:
            d_facts: list[dict[str, Any]] = []
            # Nested-folder rollup: a folder child carries no leaf
            # citations / topic-style modules but DOES persist its own
            # ``folder_stats`` + ``top_contributors`` from a prior
            # compile pass. Roll those up via phantom facts so the
            # parent's stat strip and contributor count stop showing
            # 0/0/0/0 on multi-level folder hierarchies.
            if (getattr(c, "page_type", "") or "") == "folder":
                d_facts.extend(_rollup_folder_child_phantom_facts(c.modules or []))
            for cit in c.citations or []:
                d_facts.append(
                    {
                        "fact_id": cit.id or "",
                        "memory_text": cit.text_excerpt or "",
                        "author_name": cit.author or "",
                        # Citations don't carry fact_type — leave blank
                        # so neither decision nor question buckets fire.
                        # The ``modules`` field on the child page (when
                        # present) carries the structured decision data
                        # the cross_cutting_decisions module needs.
                        "fact_type": "",
                        "message_ts": cit.timestamp or "",
                        "platform": "",
                        "permalink": cit.permalink or "",
                    }
                )
            # Promote structured module entries from each child's
            # persisted ``modules`` (if the child was compiled via the
            # modular topic path, decision/quote/tension/open-question
            # entries live there). Each promotion fires the matching
            # folder predicate (decision → cross_cutting_decisions,
            # quote → quote_highlights, tension → folder_stats tension
            # bucket, open_question → folder_stats question bucket).
            for mod in c.modules or []:
                if not isinstance(mod, dict):
                    continue
                mod_id = mod.get("id")
                inner = mod.get("data") or {}
                if not isinstance(inner, dict):
                    continue
                if mod_id == "decision_log":
                    for dec in inner.get("decisions") or []:
                        if not isinstance(dec, dict):
                            continue
                        d_facts.append(
                            {
                                "fact_id": str(dec.get("fact_id") or ""),
                                "memory_text": str(dec.get("decision") or dec.get("text") or ""),
                                "author_name": str(dec.get("made_by") or dec.get("author") or ""),
                                "fact_type": "decision",
                                "importance": dec.get("importance") or "high",
                                "message_ts": str(dec.get("date") or ""),
                            }
                        )
                elif mod_id == "quote_highlights":
                    for q in inner.get("quotes") or []:
                        if not isinstance(q, dict):
                            continue
                        text = str(q.get("text") or q.get("memory_text") or "").strip()
                        if not text:
                            continue
                        d_facts.append(
                            {
                                "fact_id": str(q.get("fact_id") or ""),
                                "memory_text": text,
                                "author_name": str(q.get("author") or q.get("made_by") or ""),
                                "fact_type": "quote",
                                "importance": q.get("importance") or "high",
                                "message_ts": str(q.get("date") or q.get("message_ts") or ""),
                            }
                        )
                elif mod_id == "tension_callout":
                    title = str(inner.get("title") or "").strip()
                    if title:
                        positions = inner.get("positions") or []
                        author = ""
                        fact_id = ""
                        if isinstance(positions, list) and positions:
                            first = positions[0]
                            if isinstance(first, dict):
                                author = str(first.get("author") or "")
                                fact_id = str(first.get("fact_id") or "")
                        d_facts.append(
                            {
                                "fact_id": fact_id,
                                "memory_text": title,
                                "author_name": author,
                                "fact_type": "tension",
                                "importance": "high",
                                "message_ts": str(inner.get("since") or ""),
                            }
                        )
                elif mod_id == "open_questions":
                    for q in inner.get("questions") or []:
                        if not isinstance(q, dict):
                            continue
                        text = str(q.get("question") or q.get("text") or "").strip()
                        if not text:
                            continue
                        d_facts.append(
                            {
                                "fact_id": str(q.get("fact_id") or ""),
                                "memory_text": text,
                                "author_name": str(q.get("raised_by") or q.get("author") or ""),
                                "fact_type": "open_question",
                                "importance": q.get("importance") or "medium",
                                "message_ts": str(q.get("raised") or q.get("date") or ""),
                            }
                        )
            descendants_payload.append(
                {
                    "title": c.title,
                    "slug": c.slug,
                    "facts": d_facts,
                }
            )

        children_payload_modular = [
            {
                "title": c.title,
                "slug": c.slug,
                "summary": (c.summary or "")[:200],
            }
            for c in children_pages
        ]

        async def _modular_llm(prompt: str) -> str:
            return await self._llm_generate_json(prompt, page_kind="topic")

        # Resolve the wikilink-target set once — used for both the prompt's
        # ``compiled_topic_titles_json`` placeholder and the post-render
        # rewrite. ``None`` (legacy callers) falls back to the children's
        # titles so the rewrite still resolves intra-folder references.
        compiled_topic_titles_safe: list[str] = [t for t in (compiled_topic_titles or []) if t] or [
            c.title for c in children_pages if c.title
        ]

        try:
            modular_out = await compile_folder_page_modular(
                folder_title=folder_title,
                folder_slug=folder_slug,
                descendants=descendants_payload,
                children=children_payload_modular,
                llm=_modular_llm,
                compiled_topic_titles=compiled_topic_titles_safe,
            )
        except Exception as exc:  # noqa: BLE001 — modular path is best-effort
            logger.warning(
                "wiki_compiler_folder_modular_failed slug=%s exc=%s",
                folder_slug,
                type(exc).__name__,
            )
            modular_out = None

        # Use the modular output unless it fell back catastrophically.
        # On a fall-back outcome we drop into the legacy prompt path
        # below so we don't ship a half-rendered dashboard.
        if modular_out is not None and not modular_out.fell_back:
            sorted_slugs = sorted(c.slug for c in children_pages if c.slug)
            fingerprint = hashlib.sha256("\n".join(sorted_slugs).encode("utf-8")).hexdigest()
            from datetime import UTC as _UTC, datetime as _dt

            children_refs = [
                WikiPageRef(
                    id=f"topic-{c.slug}" if not c.id.startswith("topic-") else c.id,
                    title=c.title,
                    slug=c.slug,
                    section_number="",
                    memory_count=c.memory_count,
                )
                for c in children_pages
            ]
            logger.info(
                "modular_folder_page_compiled folder=%s modules=%d rendered=%d",
                folder_slug,
                modular_out.planner_module_count,
                modular_out.rendered_module_count,
            )
            # Rewrite any ``[[Title]]`` references the modular narrative
            # produced into native links (compiled topics) or plain text.
            folder_content = _rewrite_topic_wikilinks(
                modular_out.content, compiled_topic_titles_safe
            )
            return WikiPage(
                id=f"folder-{folder_slug}",
                slug=folder_slug,
                title=folder_title,
                page_type="folder",
                parent_id=None,
                section_number="",
                content=folder_content,
                summary=modular_out.summary
                or f"{folder_title} — folder containing {len(children_pages)} pages.",
                memory_count=sum(c.memory_count for c in children_pages),
                last_updated=_dt.now(tz=_UTC),
                citations=[],
                children=children_refs,
                children_fingerprint=fingerprint,
                is_synthetic=True,
                modules=modular_out.modules,
            )

        # ── Legacy fallback path — preserved verbatim below ────────────
        # Build the prompt inputs from the children's existing summaries.
        # No facts loaded here — folder synthesis runs after leaf compile,
        # and the leaves already distilled the facts. Aggregating top
        # facts requires walking each child's citations, which is more
        # plumbing than the bounded folder body needs; we send empty top
        # facts on the first pass and rely on the prompt's child summaries
        # to give the LLM enough material.
        children_for_prompt = [
            {
                "title": c.title,
                "summary": (c.summary or "")[:200],
            }
            for c in children_pages
        ]
        # Aggregate entity-like signals from children's citations.
        # WikiCitation doesn't carry structured entities, but the
        # citation's ``author`` and ``media_name`` are reliable
        # entity-shaped strings the planner prompt can use as hints.
        # We dedupe and cap at 10 — pure best-effort enrichment.
        entities: list[str] = []
        seen: set[str] = set()
        for c in children_pages:
            for cit in c.citations or []:
                for candidate in (cit.author, cit.media_name):
                    if not candidate:
                        continue
                    key = candidate.strip().lower()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    entities.append(candidate.strip())
                    if len(entities) >= 10:
                        break
                if len(entities) >= 10:
                    break
            if len(entities) >= 10:
                break
        # Top facts: pull each child's first 1-2 citations as fact-shaped
        # dicts the prompt can quote. Real fact_type / quality_score
        # require a deeper data path; this gives the LLM concrete
        # material to work with rather than synthesizing from summaries
        # alone.
        top_facts: list[dict[str, Any]] = []
        for c in children_pages:
            for cit in (c.citations or [])[:1]:
                top_facts.append(
                    {
                        "memory_text": cit.text_excerpt or "",
                        "author_name": cit.author or "",
                        "fact_type": "",
                    }
                )
            if len(top_facts) >= 5:
                break

        prompt = build_folder_index_prompt(
            folder_title=folder_title,
            children=children_for_prompt,
            aggregated_entities=entities,
            top_facts=top_facts,
        )

        try:
            result = await self._call_llm(prompt, page_kind="topic")
            content = (result.content or "").strip()
            summary = (result.summary or "").strip()
        except Exception as exc:  # noqa: BLE001 — folder synthesis is best-effort
            logger.warning(
                "wiki_compiler_folder_synth_failed slug=%s exc=%s",
                folder_slug,
                type(exc).__name__,
            )
            content = ""
            summary = ""

        # Auto-TOC the children regardless of LLM success — operators
        # always need a way to navigate even if synthesis returned empty.
        children_for_toc = [
            {"title": c.title, "slug": c.slug, "summary": (c.summary or "")[:140]}
            for c in children_pages
        ]
        rendered_content = apply_children_toc_marker(content, children_for_toc)
        # Defensive: rewrite any ``[[Title]]`` references from the LLM body
        # into native links (compiled topics) or plain text.
        rendered_content = _rewrite_topic_wikilinks(rendered_content, compiled_topic_titles_safe)

        # children_fingerprint is the SHA-256 of sorted slugs — used by
        # the maintainer (Phase E) to skip re-synthesis when membership
        # is unchanged.
        sorted_slugs = sorted(c.slug for c in children_pages if c.slug)
        fingerprint = hashlib.sha256("\n".join(sorted_slugs).encode("utf-8")).hexdigest()

        # Page memory_count = sum of children memory counts (proxy for
        # "how much the folder represents"). Last_updated is now since
        # we just synthesized.
        from datetime import UTC as _UTC, datetime as _dt

        children_refs = [
            WikiPageRef(
                id=f"topic-{c.slug}" if not c.id.startswith("topic-") else c.id,
                title=c.title,
                slug=c.slug,
                section_number="",  # filled in by build_structure
                memory_count=c.memory_count,
            )
            for c in children_pages
        ]

        return WikiPage(
            id=f"folder-{folder_slug}",
            slug=folder_slug,
            title=folder_title,
            page_type="folder",
            parent_id=None,  # Set by build_structure when folder nests.
            section_number="",
            content=rendered_content,
            summary=summary or f"{folder_title} — folder containing {len(children_pages)} pages.",
            memory_count=sum(c.memory_count for c in children_pages),
            last_updated=_dt.now(tz=_UTC),
            citations=[],  # Folder citations: future — could aggregate from children.
            children=children_refs,
            children_fingerprint=fingerprint,
            is_synthetic=True,
        )

    async def compile_folders(
        self,
        *,
        plan: Any,
        leaves_by_slug: dict[str, WikiPage],
        compiled_topic_titles: list[str] | None = None,
    ) -> dict[str, WikiPage]:
        """Synthesize all folder pages from the planner output.

        ``plan`` is a ``PlannedStructure`` (duck-typed for testing —
        any object with ``folders`` and ``leaves`` attributes works).
        ``leaves_by_slug`` maps each leaf slug → its compiled WikiPage
        (the existing ``compile`` output, keyed by slug).

        Handles **nested folders** — the planner is constrained to
        depth 4, so a folder may contain other folders. Topological
        sort: process folders whose children are all already-resolved
        (leaves OR previously-compiled folders) first; iterate until
        no more progress is made. Folders that reference children we
        cannot resolve (typically because an upstream topic compile
        failed) are skipped with a log line.

        Returns a dict ``{folder_id: WikiPage}`` with one entry per
        successfully synthesized folder. When ``plan`` has no folders
        this method returns an empty dict (no LLM calls — cheap no-op).
        """
        folders = list(getattr(plan, "folders", None) or [])
        if not folders:
            return {}

        # Build a slug → folder spec lookup for the topological pass.
        folders_by_slug: dict[str, Any] = {}
        for f in folders:
            f_slug = getattr(f, "slug", None) or ""
            if f_slug:
                folders_by_slug[f_slug] = f

        out: dict[str, WikiPage] = {}
        # Reverse-lookup: folder slug → already-compiled WikiPage. Used
        # by outer folders so they can see their inner-folder children.
        folder_pages_by_slug: dict[str, WikiPage] = {}

        # Topological sort: keep iterating while we make progress.
        # Each pass synthesizes folders whose children are now ALL
        # resolvable (in leaves_by_slug OR folder_pages_by_slug). When
        # a pass produces nothing new, we stop.
        remaining: dict[str, Any] = dict(folders_by_slug)
        max_passes = len(remaining) + 2  # safety bound for cycles
        passes = 0
        while remaining and passes < max_passes:
            passes += 1
            ready_now: list[tuple[str, Any]] = []
            for f_slug, folder in remaining.items():
                child_slugs = list(getattr(folder, "child_slugs", None) or [])
                if not child_slugs:
                    # Empty folder — skip.
                    continue
                # Check every child resolves (in leaves OR already-compiled folders).
                all_ready = True
                for cs in child_slugs:
                    if cs in leaves_by_slug:
                        continue
                    if cs in folder_pages_by_slug:
                        continue
                    if cs in folders_by_slug and cs not in folder_pages_by_slug:
                        # Inner folder not yet compiled — defer.
                        all_ready = False
                        break
                    # Truly missing — neither a leaf nor a folder we know.
                    # Don't block on it; we'll log later.
                if all_ready:
                    ready_now.append((f_slug, folder))

            if not ready_now:
                # No folder is fully ready — accept the remaining ones
                # with their resolvable children only (skip un-resolvable).
                # This handles the case where some children genuinely
                # don't exist (compile failures upstream) — better to
                # produce a partial folder than no folder.
                for f_slug, folder in remaining.items():
                    ready_now.append((f_slug, folder))

            # Build the parallel task list. Children resolution is a
            # read-only walk over already-populated dicts, so it stays
            # serial; only the LLM-bound ``_compile_folder_page`` work
            # is gathered. Siblings within a single ``ready_now`` batch
            # have no inter-dependency (their children are all already
            # compiled by the topology check above), so they're safe
            # to run in parallel.
            tasks: list[tuple[str, str, list[WikiPage]]] = []
            for f_slug, folder in ready_now:
                f_title = getattr(folder, "title", None) or f_slug.replace("-", " ").title()
                child_slugs = list(getattr(folder, "child_slugs", None) or [])
                children_pages: list[WikiPage] = []
                for cs in child_slugs:
                    page = leaves_by_slug.get(cs) or folder_pages_by_slug.get(cs)
                    if page is None:
                        logger.warning(
                            "wiki_compiler_folder_missing_child folder=%s child=%s",
                            f_slug,
                            cs,
                        )
                        continue
                    children_pages.append(page)
                if not children_pages:
                    # Nothing resolvable — drop the folder entirely.
                    remaining.pop(f_slug, None)
                    continue
                tasks.append((f_slug, f_title, children_pages))

            if not tasks:
                continue

            # Cap parallelism so a wide channel (many sibling folders)
            # doesn't spike LLM concurrency past the provider's quota
            # — the existing CircuitBreaker would catch a 503 storm but
            # we'd rather not trip it on regenerate. 4 in-flight is a
            # conservative middle ground: roughly 4× speedup vs the
            # prior serial loop while leaving headroom for the topic-
            # page compile pass running on the same provider.
            sem = asyncio.Semaphore(_FOLDER_COMPILE_PARALLELISM)

            async def _compile_one(
                f_slug: str,
                f_title: str,
                children: list[WikiPage],
            ) -> tuple[str, WikiPage]:
                async with sem:
                    page = await self._compile_folder_page(
                        folder_slug=f_slug,
                        folder_title=f_title,
                        children_pages=children,
                        compiled_topic_titles=compiled_topic_titles,
                    )
                    return f_slug, page

            results = await asyncio.gather(
                *(_compile_one(s, t, cp) for s, t, cp in tasks),
            )
            # Single-threaded write-back so dict mutations don't race
            # with each other or with any concurrent reader of
            # ``folder_pages_by_slug`` from a future ``ready_now`` batch.
            for f_slug, folder_page in results:
                out[folder_page.id] = folder_page
                folder_pages_by_slug[f_slug] = folder_page
                remaining.pop(f_slug, None)
        return out

    def build_structure(
        self,
        channel_id: str,
        channel_name: str,
        platform: str,
        pages: dict[str, WikiPage],
    ) -> WikiStructure:
        nodes: list[WikiPageNode] = []
        section_counter = 0

        def _next_section() -> str:
            nonlocal section_counter
            section_counter += 1
            return str(section_counter)

        # Ordered list of fixed pages (before topics)
        _FIXED_BEFORE_TOPICS = [
            ("overview", "overview"),
        ]
        # Fixed pages after topics (order matters)
        _FIXED_AFTER_TOPICS = [
            ("people", "people"),
            ("decisions", "decisions"),
            ("faq", "faq"),
            ("glossary", "glossary"),
            ("activity", "activity"),
            ("resources", "resources"),
        ]

        # 1. Fixed pages before topics (Overview)
        for page_id, slug in _FIXED_BEFORE_TOPICS:
            if page_id in pages:
                sec = _next_section()
                p = pages[page_id]
                p.section_number = sec
                nodes.append(
                    WikiPageNode(
                        id=page_id,
                        title=self._page_title(page_id),
                        slug=slug,
                        section_number=sec,
                        page_type="fixed",
                        memory_count=p.memory_count,
                    )
                )

        # 2.x Topics — uses the current section counter for the group number
        topic_pages = sorted(
            [p for p in pages.values() if p.page_type == "topic"],
            key=lambda p: p.title,
        )
        if topic_pages:
            topic_section = _next_section()  # e.g. "2"
            for i, tp in enumerate(topic_pages, 1):
                tp.section_number = f"{topic_section}.{i}"
                topic_node = WikiPageNode(
                    id=tp.id,
                    title=tp.title,
                    slug=tp.slug,
                    section_number=f"{topic_section}.{i}",
                    page_type="topic",
                    memory_count=tp.memory_count,
                    summary=tp.summary or "",
                )
                # Nest sub-pages as children
                sub_pages = sorted(
                    [
                        p
                        for p in pages.values()
                        if p.page_type == "sub-topic" and p.parent_id == tp.id
                    ],
                    key=lambda p: p.title,
                )
                for j, sp in enumerate(sub_pages, 1):
                    sp.section_number = f"{topic_section}.{i}.{j}"
                    topic_node.children.append(
                        WikiPageNode(
                            id=sp.id,
                            title=sp.title,
                            slug=sp.slug,
                            section_number=f"{topic_section}.{i}.{j}",
                            page_type="sub-topic",
                            memory_count=sp.memory_count,
                            summary=sp.summary or "",
                        )
                    )
                nodes.append(topic_node)

        # Remaining fixed pages after topics — dynamic numbering, only if page was generated
        for page_id, slug in _FIXED_AFTER_TOPICS:
            if page_id in pages:
                sec = _next_section()
                p = pages[page_id]
                p.section_number = sec
                nodes.append(
                    WikiPageNode(
                        id=page_id,
                        title=self._page_title(page_id),
                        slug=slug,
                        section_number=sec,
                        page_type="fixed",
                        memory_count=p.memory_count,
                    )
                )

        return WikiStructure(
            channel_id=channel_id,
            channel_name=channel_name,
            platform=platform,
            pages=nodes,
        )
