"""LLM-based wiki page compiler — converts gathered data into WikiPage objects."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from beever_atlas.agents.prompt_safety import wrap_untrusted
from beever_atlas.llm import get_llm_provider
from beever_atlas.llm.model_resolver import is_ollama_model
from beever_atlas.models.domain import AtomicFact, WikiCitation, WikiPage, WikiPageNode, WikiPageRef, WikiStructure
from beever_atlas.wiki.prompts import (
    ACTIVITY_PROMPT,
    DECISIONS_PROMPT,
    FAQ_PROMPT,
    GLOSSARY_PROMPT,
    OVERVIEW_PROMPT,
    PEOPLE_PROMPT,
    RESOURCES_PROMPT,
    SUBTOPIC_PROMPT,
    TOPIC_ANALYSIS_PROMPT,
    TOPIC_PROMPT,
)
from beever_atlas.wiki.schemas import CompiledPageContent

logger = logging.getLogger(__name__)

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
    seen_urls: set[str] = set()
    for fact in facts:
        for i, url in enumerate(fact.source_media_urls):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            name = fact.source_media_names[i] if i < len(fact.source_media_names) else url.split("/")[-1]
            media.append({
                "url": url,
                "type": fact.source_media_type or "file",
                "name": name,
                "author": fact.author_name,
                "context": _truncate_context(fact.memory_text),
            })
        for j, url in enumerate(fact.source_link_urls):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            title = fact.source_link_titles[j] if j < len(fact.source_link_titles) else url
            media.append({
                "url": url,
                "type": "link",
                "name": title,
                "author": fact.author_name,
                "context": _truncate_context(fact.memory_text),
            })
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
                edges.append({
                    "source": person_name,
                    "relationship": edge_type.upper().replace("_", " "),
                    "target": target,
                })
    return edges


# Well-known generic terms to exclude from glossary (OS, common apps, hardware, generic dev tools, common infra)
# Localized titles for fixed wiki pages. Keyed by BCP-47 tag then page id.
# Missing tags fall back to English. Keep ids in sync with the WikiPage(id=...)
# values used throughout _compile_* methods.
WIKI_PAGE_TITLES: dict[str, dict[str, str]] = {
    "en":    {"overview": "Overview", "people": "People & Experts",
              "decisions": "Decisions", "faq": "FAQ", "glossary": "Glossary",
              "activity": "Recent Activity", "resources": "Resources & Media"},
    "zh-HK": {"overview": "概覽", "people": "人物與專家",
              "decisions": "決策", "faq": "常見問題", "glossary": "詞彙表",
              "activity": "近期活動", "resources": "資源與媒體"},
    "zh-TW": {"overview": "概覽", "people": "人物與專家",
              "decisions": "決策", "faq": "常見問題", "glossary": "詞彙表",
              "activity": "近期活動", "resources": "資源與媒體"},
    "zh-CN": {"overview": "概览", "people": "人物与专家",
              "decisions": "决策", "faq": "常见问题", "glossary": "词汇表",
              "activity": "近期活动", "resources": "资源与媒体"},
    "ja":    {"overview": "概要", "people": "メンバーと専門家",
              "decisions": "意思決定", "faq": "よくある質問", "glossary": "用語集",
              "activity": "最近のアクティビティ", "resources": "リソースとメディア"},
    "ko":    {"overview": "개요", "people": "인물 및 전문가",
              "decisions": "의사결정", "faq": "자주 묻는 질문", "glossary": "용어집",
              "activity": "최근 활동", "resources": "리소스 및 미디어"},
    "es":    {"overview": "Resumen", "people": "Personas y expertos",
              "decisions": "Decisiones", "faq": "Preguntas frecuentes", "glossary": "Glosario",
              "activity": "Actividad reciente", "resources": "Recursos y medios"},
    "fr":    {"overview": "Vue d'ensemble", "people": "Personnes et experts",
              "decisions": "Décisions", "faq": "FAQ", "glossary": "Glossaire",
              "activity": "Activité récente", "resources": "Ressources et médias"},
    "de":    {"overview": "Übersicht", "people": "Personen & Experten",
              "decisions": "Entscheidungen", "faq": "FAQ", "glossary": "Glossar",
              "activity": "Letzte Aktivität", "resources": "Ressourcen & Medien"},
}


GENERIC_GLOSSARY_TERMS: set[str] = {
    # Operating systems
    "windows", "macos", "linux", "ubuntu", "android", "ios",
    # Messaging / social
    "whatsapp", "imessage", "slack", "telegram", "discord", "x", "twitter",
    # Hardware
    "mac mini", "mac", "iphone", "ipad",
    # Generic dev tools
    "vs code", "visual studio code", "github", "git", "chrome", "firefox",
    # Big tech companies
    "google", "microsoft", "apple", "amazon",
    # Well-known infra / databases (generic, not channel-specific)
    "aws", "sql", "redis", "mongodb", "sqlite",
    "digital ocean", "digital ocean vps", "hetzner",
    # Common concepts that don't need defining
    "copilot",
}


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
    r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$", re.DOTALL,
)


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
    def _is_topic_relevant(cluster, channel_themes: list[str], cluster_facts: dict) -> tuple[bool, str]:
        """Check if a topic cluster should get its own page.

        Returns (should_include, skip_reason) tuple.
        """
        member_count = len(cluster_facts.get(cluster.id, []))

        # Check minimum memory threshold
        if member_count < TOPIC_MIN_MEMORY_THRESHOLD:
            return False, f"{member_count} facts, below minimum threshold of {TOPIC_MIN_MEMORY_THRESHOLD}"

        # Check relevance: topic_tags must overlap with channel themes, unless popular (5+ facts)
        if member_count >= 5:
            return True, ""

        # Normalize for comparison
        cluster_tags = {t.lower().strip() for t in (cluster.topic_tags or [])}
        theme_words = set()
        for theme in (channel_themes or []):
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

    @staticmethod
    def _postprocess_content(content: str) -> str:
        """Clean LLM output before storing as WikiPage content."""
        if not content:
            return content

        # 1. Strip terminal ## Sources / ### Sources sections
        content = WikiCompiler._SOURCES_RE.sub("", content)

        # 1b. Strip terminal numbered citation lists (e.g., "- [1] @Author ...")
        content = WikiCompiler._CITATION_LIST_RE.sub("", content)

        # 2. Sanitize mermaid blocks
        def _sanitize_node_label(match: re.Match) -> str:
            node_id = match.group(1)
            label = match.group(2)
            # Strip characters mermaid rejects inside [...]: parens, quotes, backticks
            label = re.sub(r'[()"\'\`]', " ", label)
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
                # Strip colon-style labels conservatively: only when --> NODE: free text (no brackets)
                line = re.sub(r"(-->\s*\w+(?:\[[^\]]*\])?)\s*:\s*[^\[\]|]+$", r"\1", line)
                # Sanitize node-definition labels: strip forbidden chars inside [...]
                line = re.sub(r"([A-Za-z0-9_]+)\[([^\]]*)\]", _sanitize_node_label, line)
                # Keep pipe-style labels intact: A -->|label| B is valid mermaid
                cleaned.append(line)
            return opener + "\n".join(cleaned) + closer

        content = WikiCompiler._MERMAID_BLOCK_RE.sub(_clean_mermaid, content)

        # 3. Trim over-citation: keep at most 3 consecutive [N] markers per cluster
        def _trim_citations(m: re.Match) -> str:
            markers = re.findall(r"\[\d+\]", m.group(0))
            return "".join(markers[:3])

        content = WikiCompiler._OVERCITATION_RE.sub(_trim_citations, content)

        # 4. Collapse 3+ consecutive blank lines to 2
        content = WikiCompiler._BLANK_LINES_RE.sub("\n\n\n", content)

        return content.rstrip() + "\n"

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

        # Domain-based capping
        from collections import Counter
        domain_counts: Counter[str] = Counter()
        domain_capped: list[dict] = []
        for item in filtered:
            try:
                from urllib.parse import urlparse
                host = urlparse(item.get("url", "")).hostname or ""
                domain = host.replace("www.", "")
            except Exception:
                domain = "unknown"
            cap = 10 if "github.com" in domain else 5
            if domain_counts[domain] < cap:
                domain_capped.append(item)
                domain_counts[domain] += 1

        # Total cap
        return domain_capped[:30]

    # ── LLM call ─────────────────────────────────────────────────────

    async def _llm_generate_json(self, prompt: str, temperature: float = 0.2) -> str:
        """Call the configured LLM and return raw text. Supports Gemini and Ollama."""
        if is_ollama_model(self._model_name):
            import litellm
            from beever_atlas.infra.config import get_settings
            import os
            os.environ.setdefault("OLLAMA_API_BASE", get_settings().ollama_api_base)
            resp = await litellm.acompletion(
                model=self._model_name,
                messages=[{"role": "user", "content": prompt + "\n\nRespond with valid JSON only."}],
                temperature=temperature,
                format="json",
            )
            return resp.choices[0].message.content or "{}"  # pyright: ignore[reportAttributeAccessIssue]
        else:
            from google import genai
            from google.genai import types
            client = genai.Client()
            response = await client.aio.models.generate_content(
                model=self._model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    # response_mime_type alone nudges Gemini toward JSON without
                    # forcing a schema. response_schema was tried but caused
                    # instability on very long outputs (Resources page), where
                    # the model got stuck escaping a multi-KB markdown string
                    # and emitted corrupted JSON. _parse_llm_json handles minor
                    # malformation; keep the nudge, skip the hard schema.
                    response_mime_type="application/json",
                    # Headroom for the Resources & Media page (40-row table +
                    # image/doc/link sections). Smaller pages finish well under.
                    max_output_tokens=32768,
                    temperature=temperature,
                ),
            )
            return response.text or "{}"

    async def _call_llm(self, prompt: str, max_retries: int = 1) -> CompiledPageContent:
        data: dict = {}
        for attempt in range(1 + max_retries):
            raw = await self._llm_generate_json(prompt, temperature=0.2 + (attempt * 0.1))
            parsed = _parse_llm_json(raw)
            if parsed is None:
                logger.warning(
                    "WikiCompiler: failed to parse LLM JSON (attempt %d), raw_len=%d. raw_head=%r",
                    attempt + 1,
                    len(raw or ""),
                    (raw or "")[:200],
                )
                data = {}
                if attempt < max_retries:
                    logger.info("WikiCompiler: parse failure (attempt %d), retrying...", attempt + 1)
                continue
            data = parsed if isinstance(parsed, dict) else {}
            content = data.get("content", "").strip()
            summary = data.get("summary", "").strip()
            # Return immediately on any non-empty content — don't retry just because content is short
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
        clusters_data = [
            {"id": c.id, "title": c.title, "member_count": c.member_count, "topic_tags": c.topic_tags}
            for c in clusters
        ]
        # Build media data from media_facts
        media_data = _build_media_data(gathered["media_facts"])
        # Build graph entity data
        tech_data = [{"name": t["entity"].name, "used_by": t.get("used_by", [])} for t in gathered.get("technologies", [])]
        project_data = [{"name": p["entity"].name, "deps": p.get("dependencies", []), "owners": p.get("owners", [])} for p in gathered.get("projects", [])]

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
        citation_facts = (gathered["recent_facts"] + gathered["media_facts"])[:20]
        cited_facts_for_prompt = [
            {
                "index": i,
                "author": f.author_name,
                "excerpt": wrap_untrusted(f.memory_text[:120]),
                "timestamp": f.message_ts,
            }
            for i, f in enumerate(citation_facts, 1)
        ]

        # Aggregate decisions from cluster-level data as fallback when top-level list is empty
        gathered_decisions = gathered.get("decisions", [])
        if not gathered_decisions:
            gathered_decisions = [
                d for c in gathered["clusters"]
                for d in getattr(c, "decisions", [])
            ]

        prompt = self._fmt_prompt(OVERVIEW_PROMPT,
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
        result = await self._call_llm(prompt)
        return WikiPage(
            id="overview",
            slug="overview",
            title=self._page_title("overview"),
            page_type="fixed",
            section_number="1",
            content=self._postprocess_content(result.content),
            summary=result.summary,
            memory_count=gathered["total_facts"],
            citations=_build_citations(citation_facts),
        )

    async def _analyze_topic(self, cluster, sorted_facts: list[AtomicFact]) -> dict | None:
        """Analyze a large topic cluster to decide if it needs sub-pages.

        Returns the parsed analysis dict or None if analysis fails or isn't needed.
        """
        indexed_facts = [
            {"index": i, "memory_text": wrap_untrusted(f.memory_text), "author_name": f.author_name, "fact_type": f.fact_type}
            for i, f in enumerate(sorted_facts[:30])
        ]
        prompt = self._fmt_prompt(TOPIC_ANALYSIS_PROMPT,
            title=cluster.title,
            summary=cluster.summary,
            fact_count=len(sorted_facts),
            indexed_facts_json=json.dumps(indexed_facts, default=str),
        )
        try:
            raw = await self._llm_generate_json(prompt)
            data = json.loads(raw)
            if not isinstance(data, dict) or "needs_subpages" not in data:
                logger.warning("WikiCompiler: topic analysis returned invalid structure for %s", cluster.title)
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
        prompt = self._fmt_prompt(SUBTOPIC_PROMPT,
            parent_title=parent_title,
            title=sub_title,
            summary=sub_info.get("summary", ""),
            fact_count=fact_count,
            member_facts_json=json.dumps(facts_data, default=str),
            media_json=json.dumps(media_data, default=str),
        )
        result = await self._call_llm(prompt, max_retries=2)
        content = self._postprocess_content(result.content)
        if not content or len(content.strip()) < 50:
            content = _facts_fallback_content(sub_facts)
        page_id = f"topic-{parent_slug}--{sub_slug}"
        return WikiPage(
            id=page_id,
            slug=f"{parent_slug}--{sub_slug}",
            title=sub_title,
            page_type="sub-topic",
            parent_id=f"topic-{parent_slug}",
            content=content,
            summary=result.summary,
            memory_count=fact_count,
            citations=_build_citations(sub_facts[:10]),
        )

    async def _compile_topic_page(self, cluster, gathered: dict) -> WikiPage | list[WikiPage]:
        """Compile a topic page. Returns a single page or [parent, *sub_pages] for large topics."""
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
                    related_topics.append({"id": f"topic-{_slugify(rc.title) or rc.id}", "title": rc.title})
                    break
        related_topics_json = json.dumps(related_topics, default=str)

        # Sub-page analysis for large clusters
        if len(member_facts) >= TOPIC_SUBPAGE_THRESHOLD:
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
                            logger.warning("WikiCompiler: sub-page failed for topic %s: %s", cluster.title, res)
                        else:
                            sub_pages.append(res)

                    # Filter out empty/minimal sub-pages (< 50 chars of content)
                    valid_sub_pages: list[WikiPage] = []
                    for sp in sub_pages:
                        if len(sp.content.strip()) >= 50:
                            valid_sub_pages.append(sp)
                        else:
                            logger.info("WikiCompiler: discarding empty sub-page '%s' for topic '%s'", sp.title, cluster.title)
                    sub_pages = valid_sub_pages

                    if sub_pages:
                        # Build parent overview page (without full detail — sub-pages have that)
                        parent_prompt = self._fmt_prompt(TOPIC_PROMPT,
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
                        parent_result = await self._call_llm(parent_prompt)
                        children_refs = [
                            WikiPageRef(
                                id=sp.id, title=sp.title, slug=sp.slug,
                                section_number="", memory_count=sp.memory_count,
                            )
                            for sp in sub_pages
                        ]
                        parent_page = WikiPage(
                            id=f"topic-{slug}",
                            slug=slug,
                            title=cluster.title,
                            page_type="topic",
                            content=parent_result.content,
                            summary=parent_result.summary,
                            memory_count=cluster.member_count,
                            citations=_build_citations(sorted_facts[:20]),
                            children=children_refs,
                        )
                        return [parent_page, *sub_pages]
                except Exception as exc:
                    logger.warning(
                        "WikiCompiler: sub-page generation failed for %s, falling back to flat page: %s",
                        cluster.title, exc,
                    )

        # Flat topic page (default path, or fallback from failed sub-page generation)
        prompt = self._fmt_prompt(TOPIC_PROMPT,
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
        result = await self._call_llm(prompt)
        content = self._postprocess_content(result.content)
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
            citations=_build_citations(sorted_facts[:20]),
        )

    async def _compile_people(self, gathered: dict) -> WikiPage:
        channel_summary = gathered["channel_summary"]
        relationship_edges = _format_relationship_edges(gathered["persons"])
        prompt = self._fmt_prompt(PEOPLE_PROMPT,
            persons_json=json.dumps(gathered["persons"], default=str),
            top_people_json=json.dumps(channel_summary.top_people, default=str),
            relationship_edges_json=json.dumps(relationship_edges, default=str),
        )
        result = await self._call_llm(prompt)
        return WikiPage(
            id="people",
            slug="people",
            title=self._page_title("people"),
            page_type="fixed",
            content=self._postprocess_content(result.content),
            summary=result.summary,
            memory_count=len(gathered["persons"]),
        )

    async def _compile_decisions(self, gathered: dict) -> WikiPage:
        channel_summary = gathered["channel_summary"]
        prompt = self._fmt_prompt(DECISIONS_PROMPT,
            decisions_json=json.dumps(gathered["decisions"], default=str),
            top_decisions_json=json.dumps(channel_summary.top_decisions, default=str),
        )
        result = await self._call_llm(prompt)
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
                faq_by_topic.append({
                    "topic": cluster.title,
                    "questions": cluster.faq_candidates,
                })
                topic_names.append(cluster.title)

        prompt = self._fmt_prompt(FAQ_PROMPT,
            faq_candidates_json=json.dumps(faq_by_topic, default=str),
            topic_names_json=json.dumps(topic_names, default=str),
        )
        result = await self._call_llm(prompt)
        return WikiPage(
            id="faq",
            slug="faq",
            title=self._page_title("faq"),
            page_type="fixed",
            content=self._postprocess_content(result.content),
            summary=result.summary,
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
            t for t in glossary_terms
            if (t.lower() if isinstance(t, str) else str(t).lower()) not in GENERIC_GLOSSARY_TERMS
        ]

        # Cap at 30 terms
        glossary_terms = glossary_terms[:30]

        prompt = self._fmt_prompt(GLOSSARY_PROMPT,
            glossary_terms_json=json.dumps(glossary_terms, default=str),
            channel_description=channel_summary.description or channel_summary.channel_name,
        )
        result = await self._call_llm(prompt)
        return WikiPage(
            id="glossary",
            slug="glossary",
            title=self._page_title("glossary"),
            page_type="fixed",
            content=self._postprocess_content(result.content),
            summary=result.summary,
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
        media_data = _build_media_data(media_facts)
        media_data = self._filter_media_for_resources(media_data)

        content = _assemble_resources_markdown(media_data)
        summary = (
            f"Catalog of {len(media_data)} shared resource(s) across "
            f"{len({item['type'] for item in media_data})} type(s)."
            if media_data else "No shared resources found."
        )

        return WikiPage(
            id="resources",
            slug="resources",
            title=self._page_title("resources"),
            page_type="fixed",
            content=content,
            summary=summary,
            memory_count=len(media_data),
            citations=_build_citations(media_facts[:20]),
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

        prompt = self._fmt_prompt(ACTIVITY_PROMPT,
            recent_facts_json=json.dumps(recent_data, default=str),
            recent_activity_json=json.dumps(channel_summary.recent_activity_summary, default=str),
            recent_media_json=json.dumps(recent_media, default=str),
        )
        result = await self._call_llm(prompt)
        return WikiPage(
            id="activity",
            slug="activity",
            title=self._page_title("activity"),
            page_type="fixed",
            content=self._postprocess_content(result.content),
            summary=result.summary,
            memory_count=len(gathered["recent_facts"]),
        )

    async def _translate_cluster_titles(
        self, clusters: list
    ) -> dict[str, str]:
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
            'Output JSON shape:\n'
            '{"titles": [{"id": "<cluster_id>", "title": "<translated>"}]}'
        )
        try:
            raw = await self._llm_generate_json(prompt, temperature=0.2)
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
                        "WikiCompiler: unknown cluster id %s in translation response", cid,
                    )
                    continue
                if cid in out:
                    logger.warning(
                        "WikiCompiler: duplicate cluster id %s in translation response", cid,
                    )
                    continue
                stripped = title.strip()
                original = originals[cid]
                if _looks_like_handle(original) and stripped != original:
                    logger.warning(
                        "WikiCompiler: rejecting suspicious title translation for handle-like id %s (%r -> %r)",
                        cid, original, stripped,
                    )
                    continue
                # Length sanity: reject pathological expansions (>3x char length).
                if len(stripped) > max(40, 3 * len(original)):
                    logger.warning(
                        "WikiCompiler: rejecting over-long title translation for %s (%d -> %d chars)",
                        cid, len(original), len(stripped),
                    )
                    continue
                out[cid] = stripped
            logger.info(
                "WikiCompiler: translated %d/%d topic titles to %s",
                len(out), len(pairs), self._target_lang,
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
        clusters = gathered["clusters"]
        channel_summary = gathered["channel_summary"]

        # Localize topic titles when rendering in a different target language.
        # Each cluster is shallow-copied with an updated title so the rest of
        # this compile sees translated titles without touching the DB-backing
        # TopicCluster objects (they're pydantic; model_copy returns a clone).
        title_map = await self._translate_cluster_titles(clusters)
        if title_map:
            clusters = [
                c.model_copy(update={"title": title_map[c.id]}) if c.id in title_map else c
                for c in clusters
            ]
            gathered = {**gathered, "clusters": clusters}
        pages: dict[str, WikiPage] = {}
        pages_completed: list[str] = []

        async def _tracked(coro, page_key: str):
            """Wrap a compile coroutine to track completion."""
            result = await coro
            pages_completed.append(page_key)
            if on_page_compiled:
                await on_page_compiled(page_key, len(pages_completed), list(pages_completed))
            return result

        # Build list of (key, coroutine) pairs, gating conditional pages BEFORE dispatching LLM calls
        fixed_tasks: list[tuple[str, Any]] = []

        # Always generate: overview, people, activity
        fixed_tasks.append(("overview", _tracked(self._compile_overview(gathered), "overview")))
        fixed_tasks.append(("people", _tracked(self._compile_people(gathered), "people")))

        # Conditional: decisions — skip if 0 decisions
        if len(gathered.get("decisions", [])) > 0:
            fixed_tasks.append(("decisions", _tracked(self._compile_decisions(gathered), "decisions")))
        else:
            logger.info("WikiCompiler: skipping Decisions page (0 decisions)")

        # Conditional: FAQ — skip if 0 faq_candidates across all clusters
        total_faq = sum(len(c.faq_candidates) for c in clusters)
        if total_faq > 0:
            fixed_tasks.append(("faq", _tracked(self._compile_faq(gathered), "faq")))
        else:
            logger.info("WikiCompiler: skipping FAQ page (0 faq candidates)")

        # Conditional: glossary — skip if 0 glossary_terms
        if len(channel_summary.glossary_terms or []) > 0:
            fixed_tasks.append(("glossary", _tracked(self._compile_glossary(gathered), "glossary")))
        else:
            logger.info("WikiCompiler: skipping Glossary page (0 glossary terms)")

        # Always generate: activity
        fixed_tasks.append(("activity", _tracked(self._compile_activity(gathered), "activity")))

        # Conditional: resources — skip if 0 media
        media_data = _build_media_data(gathered.get("media_facts", []))
        if len(media_data) > 0:
            fixed_tasks.append(("resources", _tracked(self._compile_resources(gathered), "resources")))
        else:
            logger.info("WikiCompiler: skipping Resources page (0 media)")

        # Filter clusters: skip thin or off-topic topics
        channel_themes = channel_summary.themes if hasattr(channel_summary, "themes") else []
        if isinstance(channel_themes, str):
            channel_themes = [channel_themes]
        filtered_clusters: list = []
        skipped_topics: list[dict] = []
        for c in clusters:
            should_include, skip_reason = self._is_topic_relevant(c, channel_themes, gathered["cluster_facts"])
            if should_include:
                filtered_clusters.append(c)
            else:
                logger.info("WikiCompiler: skipping topic '%s' (%s)", c.title, skip_reason)
                skipped_topics.append({"title": c.title, "reason": skip_reason, "member_count": c.member_count})

        # Store skipped topics so overview can reference them
        gathered["_skipped_topics"] = skipped_topics

        topic_tasks = [
            (f"topic-{_slugify(c.title) or c.id}", _tracked(self._compile_topic_page(c, gathered), f"topic-{_slugify(c.title) or c.id}"))
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
                        id=page_id, title=self._page_title(page_id), slug=slug,
                        section_number=sec, page_type="fixed",
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
                    id=tp.id, title=tp.title, slug=tp.slug,
                    section_number=f"{topic_section}.{i}",
                    page_type="topic",
                    memory_count=tp.memory_count,
                )
                # Nest sub-pages as children
                sub_pages = sorted(
                    [p for p in pages.values() if p.page_type == "sub-topic" and p.parent_id == tp.id],
                    key=lambda p: p.title,
                )
                for j, sp in enumerate(sub_pages, 1):
                    sp.section_number = f"{topic_section}.{i}.{j}"
                    topic_node.children.append(
                        WikiPageNode(
                            id=sp.id, title=sp.title, slug=sp.slug,
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
                        id=page_id, title=self._page_title(page_id), slug=slug,
                        section_number=sec, page_type="fixed",
                        memory_count=p.memory_count,
                    )
                )

        return WikiStructure(
            channel_id=channel_id,
            channel_name=channel_name,
            platform=platform,
            pages=nodes,
        )
