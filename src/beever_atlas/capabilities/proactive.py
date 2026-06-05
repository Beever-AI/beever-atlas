"""Proactive intelligence helpers for the chat reply path.

Surfaces documented TENSIONS (open disagreements) that are relevant to an
answer, so the bot can warn inline when a reply touches contested ground. Reuses
the wiki ``tension_callout`` modules (the same source as the ``get_tensions`` MCP
tool). ACL-enforced and never raises — returns ``[]`` on denial or any error, so
the reply path can call it best-effort.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9'\-]{3,}")
_STOPWORDS = {
    "this",
    "that",
    "these",
    "those",
    "with",
    "from",
    "about",
    "their",
    "there",
    "have",
    "will",
    "would",
    "should",
    "could",
    "what",
    "when",
    "where",
    "which",
    "into",
    "than",
    "then",
    "they",
    "them",
    "your",
    "ours",
    "been",
    "were",
    "also",
    "does",
    "done",
    "such",
    "some",
    "more",
    "most",
    "over",
    "under",
    "here",
}


def _keywords(text: str) -> set[str]:
    """Significant lowercase word stems from text (len>=4, minus stopwords)."""
    return {w.lower() for w in _WORD_RE.findall(text or "")} - _STOPWORDS


def _summarize_positions(item: dict) -> str:
    """Build a short 'A: stance vs B: stance' detail, or fall back to status."""
    parts: list[str] = []
    for pos in (item.get("positions") or [])[:2]:
        if not isinstance(pos, dict):
            continue
        author = (pos.get("author") or "").strip()
        stance = (pos.get("stance") or "").strip()
        if author and stance:
            parts.append(f"{author}: {stance}")
        elif stance:
            parts.append(stance)
    if parts:
        return " vs ".join(parts)
    status = (item.get("status") or "").strip()
    return f"status: {status}" if status else ""


async def get_relevant_tensions(
    channel_id: str,
    principal_id: str,
    answer_text: str,
    *,
    limit: int = 2,
) -> list[dict]:
    """Documented tensions whose topic overlaps the answer.

    Relevance is keyword overlap between each tension's title and the answer,
    so an answer about an unrelated topic surfaces NO tension (avoids false
    alarms). Returns at most ``limit`` ``{title, detail}`` dicts; ``[]`` on
    access denial or any error.
    """
    if not channel_id or not principal_id:
        return []
    try:
        from beever_atlas.infra.channel_access import assert_channel_access

        await assert_channel_access(principal_id, channel_id)
    except Exception:
        return []

    try:
        from beever_atlas.stores import get_stores
        from beever_atlas.wiki.page_store import WikiPageStore

        page_store = WikiPageStore(db=get_stores().mongodb.db)
        pages = await page_store.list_pages(channel_id, target_lang="en")
    except Exception:
        logger.debug("get_relevant_tensions: page load failed channel=%s", channel_id)
        return []

    return extract_relevant_tensions(pages, answer_text, limit=limit)


def extract_relevant_tensions(pages, answer_text: str, *, limit: int = 2) -> list[dict]:
    """Pure core: scan wiki pages for ``tension_callout`` modules and keep those
    whose title keywords overlap the answer. Each ``page`` only needs a
    ``modules`` attribute. Returns at most ``limit`` ``{title, detail}`` dicts.
    """
    answer_kw = _keywords(answer_text)
    if not answer_kw:
        return []

    out: list[dict] = []
    for page in pages:
        for module in getattr(page, "modules", None) or []:
            if not isinstance(module, dict) or module.get("id") != "tension_callout":
                continue
            data = module.get("data") or {}
            if not isinstance(data, dict):
                continue
            raw = data.get("tensions")
            items = raw if isinstance(raw, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = (item.get("title") or item.get("summary") or "").strip()
                if not title:
                    continue
                # Only surface a tension the answer actually touches.
                if answer_kw.isdisjoint(_keywords(title)):
                    continue
                out.append({"title": title, "detail": _summarize_positions(item)})
                if len(out) >= limit:
                    return out
    return out
