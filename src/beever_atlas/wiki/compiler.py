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

# Minimum number of member facts for a cluster to get its own topic page
TOPIC_MIN_MEMORY_THRESHOLD = 3


def _slugify(text: str) -> str:
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug.strip())
    return slug[:80]


def _build_permalink(fact: AtomicFact) -> str:
    """Build a best-effort permalink to the original message."""
    if not fact.source_message_id and not fact.message_ts:
        return ""
    # For Slack: https://slack.com/archives/{channel}/{message_ts}
    if fact.platform == "slack" and fact.channel_id:
        ts = fact.message_ts.replace(".", "p") if fact.message_ts else ""
        if ts:
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
    except Exception:
        return url


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
            media.append(
                {
                    "url": url,
                    "type": fact.source_media_type or "file",
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
            media.append(
                {
                    "url": url,
                    "type": "link",
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
            doc_lines.append(f"\n**{name}** — {ctx} [Download]({url})")
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
            vid_lines.append(f"\n**{desc}** [Watch]({url})")
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
    },
    "zh-HK": {
        "overview": "概覽",
        "people": "人物與專家",
        "decisions": "決策",
        "faq": "常見問題",
        "glossary": "詞彙表",
        "activity": "近期活動",
        "resources": "資源與媒體",
    },
    "zh-TW": {
        "overview": "概覽",
        "people": "人物與專家",
        "decisions": "決策",
        "faq": "常見問題",
        "glossary": "詞彙表",
        "activity": "近期活動",
        "resources": "資源與媒體",
    },
    "zh-CN": {
        "overview": "概览",
        "people": "人物与专家",
        "decisions": "决策",
        "faq": "常见问题",
        "glossary": "词汇表",
        "activity": "近期活动",
        "resources": "资源与媒体",
    },
    "ja": {
        "overview": "概要",
        "people": "メンバーと専門家",
        "decisions": "意思決定",
        "faq": "よくある質問",
        "glossary": "用語集",
        "activity": "最近のアクティビティ",
        "resources": "リソースとメディア",
    },
    "ko": {
        "overview": "개요",
        "people": "인물 및 전문가",
        "decisions": "의사결정",
        "faq": "자주 묻는 질문",
        "glossary": "용어집",
        "activity": "최근 활동",
        "resources": "리소스 및 미디어",
    },
    "es": {
        "overview": "Resumen",
        "people": "Personas y expertos",
        "decisions": "Decisiones",
        "faq": "Preguntas frecuentes",
        "glossary": "Glosario",
        "activity": "Actividad reciente",
        "resources": "Recursos y medios",
    },
    "fr": {
        "overview": "Vue d'ensemble",
        "people": "Personnes et experts",
        "decisions": "Décisions",
        "faq": "FAQ",
        "glossary": "Glossaire",
        "activity": "Activité récente",
        "resources": "Ressources et médias",
    },
    "de": {
        "overview": "Übersicht",
        "people": "Personen & Experten",
        "decisions": "Entscheidungen",
        "faq": "FAQ",
        "glossary": "Glossar",
        "activity": "Letzte Aktivität",
        "resources": "Ressourcen & Medien",
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


def _collect_glossary_entries(glossary_terms: list, clusters: list) -> list[dict]:
    """Aggregate {term, definition, first_mentioned_by, related_topics} rows."""
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
                "related_topics": list(t.get("related_topics") or []),
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
    # Enrich from cluster key_entities.
    for c in clusters or []:
        cluster_title = (getattr(c, "title", "") or "").strip()
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


def _render_glossary_terms_table(entries: list[dict]) -> str:
    if not entries:
        return ""
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
        related = ", ".join(row["related_topics"][:4]) if row["related_topics"] else "—"
        related = related.replace("|", "\\|")
        lines.append(f"| {term} | {definition} | {author} | {related} |")
    return "\n".join(lines)


def _splice_glossary_sections(content: str, glossary_terms: list, clusters: list) -> str:
    """Append deterministic Introduction + Terms table when the LLM drops them.

    The Glossary prompt sometimes emits only the relationship mermaid diagram
    and nothing else. This helper detects missing Introduction and Terms
    sections via heading-alias regex and appends deterministic replacements.
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
        entries = _collect_glossary_entries(glossary_terms, clusters)
        block = _render_glossary_terms_table(entries)
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
        cluster, channel_themes: list[str], cluster_facts: dict
    ) -> tuple[bool, str]:
        """Check if a topic cluster should get its own page.

        Returns (should_include, skip_reason) tuple.
        """
        member_count = len(cluster_facts.get(cluster.id, []))

        # Check minimum memory threshold
        if member_count < TOPIC_MIN_MEMORY_THRESHOLD:
            return (
                False,
                f"{member_count} facts, below minimum threshold of {TOPIC_MIN_MEMORY_THRESHOLD}",
            )

        # Check relevance: topic_tags must overlap with channel themes, unless popular (5+ facts)
        if member_count >= 5:
            return True, ""

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
        for m in cls._INLINE_CITATION_RE.finditer(content):
            try:
                used_indices.add(int(m.group(1)))
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
            # rejects in labels like `studio.votee.ai`, `CI/CD pipeline`).
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
            except Exception:
                pass

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
            except Exception:
                domain = "unknown"
            if domain in _SOCIAL_DOMAINS:
                cap = 5
            elif "github.com" in domain:
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
        "analysis": 4096,
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
            import litellm
            import os

            os.environ.setdefault("OLLAMA_API_BASE", settings.ollama_api_base)
            if use_delimited:
                resp = await litellm.acompletion(
                    model=self._model_name,
                    messages=[{"role": "user", "content": prompt + _DELIMITED_RESPONSE_SUFFIX}],
                    temperature=temperature,
                )
            else:
                resp = await litellm.acompletion(
                    model=self._model_name,
                    messages=[
                        {"role": "user", "content": prompt + "\n\nRespond with valid JSON only."}
                    ],
                    temperature=temperature,
                    format="json",
                )
            return resp.choices[0].message.content or "{}"  # pyright: ignore[reportAttributeAccessIssue]
        else:
            from google import genai
            from google.genai import types

            client = genai.Client()
            if use_delimited:
                config = types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                )
                contents = prompt + _DELIMITED_RESPONSE_SUFFIX
            else:
                config = types.GenerateContentConfig(
                    # response_mime_type alone nudges Gemini toward JSON without
                    # forcing a schema. response_schema was tried but caused
                    # instability on very long outputs (Resources page), where
                    # the model got stuck escaping a multi-KB markdown string
                    # and emitted corrupted JSON. _parse_llm_json handles minor
                    # malformation; keep the nudge, skip the hard schema.
                    response_mime_type="application/json",
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                )
                contents = prompt
            response = await client.aio.models.generate_content(
                model=self._model_name,
                contents=contents,
                config=config,
            )
            return response.text or "{}"

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
        try:
            raw = await self._llm_generate_json(prompt, page_kind="analysis")
            data = json.loads(raw)
            if not isinstance(data, dict) or "needs_subpages" not in data:
                logger.warning(
                    "WikiCompiler: topic analysis returned invalid structure for %s", cluster.title
                )
                return None
            return data
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("WikiCompiler: topic analysis failed for %s: %s", cluster.title, exc)
            return None

    async def _compile_subtopic_page(
        self,
        parent_slug: str,
        parent_title: str,
        sub_info: dict,
        all_sorted_facts: list[AtomicFact],
    ) -> WikiPage:
        """Compile a single sub-topic page from a subset of facts."""
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

    async def _compile_topic_page(self, cluster, gathered: dict) -> WikiPage | list[WikiPage]:
        """Compile a topic page. Returns a single page or [parent, *sub_pages] for large topics."""
        member_facts: list[AtomicFact] = gathered["cluster_facts"].get(cluster.id, [])
        # Phase 4: thin-topic routing (only when wiki_compiler_v2=ON).
        from beever_atlas.infra.config import get_settings

        v2 = get_settings().wiki_compiler_v2
        if v2 and len(member_facts) < _THIN_TOPIC_THRESHOLD:
            return await self._compile_thin_topic(cluster, gathered)
        sorted_facts = sorted(member_facts, key=lambda f: f.quality_score, reverse=True)
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
        slug = _slugify(cluster.title) or cluster.id

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

        # Sub-page analysis for large clusters
        if len(member_facts) >= TOPIC_SUBPAGE_THRESHOLD:
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
            if analysis and analysis.get("needs_subpages") and analysis.get("subpages"):
                try:
                    # Generate sub-pages in parallel
                    sub_coros = [
                        self._compile_subtopic_page(slug, cluster.title, sub_info, sorted_facts)
                        for sub_info in analysis["subpages"]
                    ]
                    sub_results = await asyncio.gather(*sub_coros, return_exceptions=True)
                    sub_pages: list[WikiPage] = []
                    for res in sub_results:
                        if isinstance(res, BaseException):
                            logger.warning(
                                "WikiCompiler: sub-page failed for topic %s: %s", cluster.title, res
                            )
                        else:
                            sub_pages.append(res)

                    # Filter out empty/minimal sub-pages (< 50 chars of content)
                    valid_sub_pages: list[WikiPage] = []
                    for sp in sub_pages:
                        if len(sp.content.strip()) >= 50:
                            valid_sub_pages.append(sp)
                        else:
                            logger.info(
                                "WikiCompiler: discarding empty sub-page '%s' for topic '%s'",
                                sp.title,
                                cluster.title,
                            )
                    sub_pages = valid_sub_pages

                    if sub_pages:
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
                            key_relationships_json=json.dumps(
                                cluster.key_relationships, default=str
                            ),
                            member_facts_json=json.dumps(facts_data, default=str),
                            media_json=json.dumps(media_data, default=str),
                            related_topics_json=related_topics_json,
                        )
                        parent_result = await self._call_llm(parent_prompt, page_kind="topic")
                        parent_content = parent_result.content
                        if v2:
                            parent_content = self._postprocess_content(parent_content)
                            parent_content = _splice_key_facts_table(
                                parent_content, cluster.key_facts
                            )
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
                        final_parent_content = parent_content if v2 else parent_result.content
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
        try:
            relationship_edges = _format_relationship_edges(persons)
            prompt = self._fmt_prompt(
                PEOPLE_PROMPT,
                persons_json=json.dumps(persons, default=str),
                top_people_json=json.dumps(channel_summary.top_people, default=str),
                relationship_edges_json=json.dumps(relationship_edges, default=str),
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
        return WikiPage(
            id="people",
            slug="people",
            title=self._page_title("people"),
            page_type="fixed",
            content=self._postprocess_content(content),
            summary=summary_text,
            memory_count=len(persons),
        )

    async def _compile_decisions(self, gathered: dict) -> WikiPage:
        channel_summary = gathered["channel_summary"]
        prompt = self._fmt_prompt(
            DECISIONS_PROMPT,
            decisions_json=json.dumps(gathered["decisions"], default=str),
            top_decisions_json=json.dumps(channel_summary.top_decisions, default=str),
        )
        result = await self._call_llm(prompt, page_kind="decisions")
        return WikiPage(
            id="decisions",
            slug="decisions",
            title=self._page_title("decisions"),
            page_type="fixed",
            content=self._postprocess_content(result.content),
            summary=result.summary,
            memory_count=len(gathered["decisions"]),
        )

    async def _compile_faq(self, gathered: dict) -> WikiPage:
        """Compile FAQ page from aggregated faq_candidates across all TopicClusters."""
        clusters = gathered["clusters"]
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
        return WikiPage(
            id="faq",
            slug="faq",
            title=self._page_title("faq"),
            page_type="fixed",
            content=self._postprocess_content(content),
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

        prompt = self._fmt_prompt(
            GLOSSARY_PROMPT,
            glossary_terms_json=json.dumps(glossary_terms, default=str),
            channel_description=channel_summary.description or channel_summary.channel_name,
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
            content, glossary_terms, gathered.get("clusters", []) or []
        )
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
        return WikiPage(
            id="activity",
            slug="activity",
            title=self._page_title("activity"),
            page_type="fixed",
            content=self._postprocess_content(content),
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

        # Filter clusters: skip thin or off-topic topics.
        # Use original clusters for relevance check (titles are cosmetic only).
        channel_themes = channel_summary.themes if hasattr(channel_summary, "themes") else []
        if isinstance(channel_themes, str):
            channel_themes = [channel_themes]
        filtered_clusters: list = []
        skipped_topics: list[dict] = []
        for c in clusters:
            should_include, skip_reason = self._is_topic_relevant(
                c, channel_themes, gathered["cluster_facts"]
            )
            if should_include:
                filtered_clusters.append(c)
            else:
                logger.info("WikiCompiler: skipping topic '%s' (%s)", c.title, skip_reason)
                skipped_topics.append(
                    {"title": c.title, "reason": skip_reason, "member_count": c.member_count}
                )

        # Store skipped topics so overview can reference them
        gathered["_skipped_topics"] = skipped_topics

        if parallel_dispatch:

            async def _compile_topic_with_titles(cluster):
                updated_gathered = await _apply_titles_to_gathered()
                updated_gathered["_skipped_topics"] = skipped_topics
                return await self._compile_topic_page(cluster, updated_gathered)

            topic_tasks = [
                (
                    f"topic-{_slugify(c.title) or c.id}",
                    _tracked(_compile_topic_with_titles(c), f"topic-{_slugify(c.title) or c.id}"),
                )
                for c in filtered_clusters
            ]
        else:
            topic_tasks = [
                (
                    f"topic-{_slugify(c.title) or c.id}",
                    _tracked(
                        self._compile_topic_page(c, gathered), f"topic-{_slugify(c.title) or c.id}"
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
