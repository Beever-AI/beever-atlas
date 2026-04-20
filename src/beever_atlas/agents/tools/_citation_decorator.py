"""Tool output decorator that feeds the SourceRegistry.

Usage:
    @cite_tool_output(kind="channel_message")
    async def search_channel_facts(...) -> list[dict]:
        ...

When the feature flag is off (or no registry is bound to the current
context), the decorator is a transparent no-op — the underlying tool
returns unmodified results and no registration happens.

When enabled:
- iterates the tool's list-of-dicts result
- derives a `native_identity` string per kind
- builds `MediaAttachment`s from per-kind fields
- registers with the current SourceRegistry
- injects `_cite` and `_src_id` annotations into each dict the LLM sees
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Awaitable, Callable, Iterable

from beever_atlas.agents.citations.registry import current_registry
from beever_atlas.agents.citations.types import (
    MediaAttachment,
    SupportedKind,
)

logger = logging.getLogger(__name__)


def cite_tool_output(
    *,
    kind: SupportedKind,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator factory bound to a citation kind.

    Supports three tool return shapes:
    - `list[dict]`: every dict is annotated with `_cite` / `_src_id`.
    - `dict` with a single list-valued key (`results`, `items`, `data`):
      the inner list is unwrapped, its dicts annotated, and re-wrapped.
    - `dict` that represents one source: the dict itself is annotated.

    When no registry is bound (flag off or outside a QA turn), the
    decorator is a transparent no-op.
    """

    def decorator(
        fn: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = await fn(*args, **kwargs)
            registry = current_registry()
            if registry is None:
                return result
            tool_name = fn.__name__
            # Bind positional + keyword args to parameter names in one pass
            # so downstream helpers see a uniform `kwargs`-shape mapping.
            bound = _bind_call_args(fn, args, kwargs)
            query = _extract_query_from_args(bound)
            ctx = _extract_context_from_args(bound)

            if isinstance(result, list):
                for item in result:
                    if not isinstance(item, dict):
                        continue
                    if item.get("_empty"):
                        continue
                    _annotate(item, kind, tool_name, query, registry, ctx)
                return result

            if isinstance(result, dict):
                # Unwrap list-valued envelopes first.
                for key in ("results", "items", "data"):
                    inner = result.get(key)
                    if (
                        isinstance(inner, list)
                        and inner
                        and all(isinstance(x, dict) for x in inner)
                    ):
                        for item in inner:
                            _annotate(item, kind, tool_name, query, registry, ctx)
                        return result
                # Single-source dict — annotate the whole dict.
                _annotate(result, kind, tool_name, query, registry, ctx)
                return result

            return result

        # Stamp a sentinel attribute so `verify_tool_coverage()` can detect
        # our decorator specifically (generic `__wrapped__` check would
        # false-positive on any `functools.wraps`-based decorator).
        wrapped._cite_tool_kind = kind  # type: ignore[attr-defined]
        return wrapped

    return decorator


def _bind_call_args(fn: Callable[..., Any], args: tuple, kwargs: dict) -> dict[str, Any]:
    """Bind `args`/`kwargs` to `fn`'s parameter names. Returns a flat name→value
    dict. Uses `bind_partial` so optional params are simply absent rather than
    raising. Never throws; falls back to plain kwargs on any reflection error.
    """
    try:
        sig = inspect.signature(fn)
        bound = sig.bind_partial(*args, **kwargs)
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return dict(kwargs)


def _extract_context_from_args(bound: dict[str, Any]) -> dict[str, Any]:
    """Pull channel_id / page_type / topic hints out of the call so the
    decorator can enrich single-source dict returns whose fields don't
    already carry enough to derive a native identity.
    """
    out: dict[str, Any] = {}
    for key in ("channel_id", "page_type", "topic", "topic_name", "entities"):
        if key in bound and bound[key] is not None:
            out[key] = bound[key]
    return out


# ---- annotation --------------------------------------------------------


def _annotate(
    item: dict,
    kind: SupportedKind,
    tool_name: str,
    query: str,
    registry: Any,
    ctx: dict[str, Any] | None = None,
) -> None:
    try:
        ctx = ctx or {}
        # Inject caller-context fields into the item so identity/native
        # helpers see them (non-destructive; preserves existing values).
        for k in ("channel_id", "page_type", "topic", "topic_name"):
            if k in ctx and k not in item:
                item[k] = ctx[k]
        native_identity = _derive_native_identity(kind, item)
        if not native_identity:
            return
        excerpt = _extract_excerpt(kind, item)
        if not excerpt:
            return
        title = _extract_title(kind, item)
        retrieved_by = {
            "tool": tool_name,
            "query": query,
            "score": _normalize_score(item.get("confidence", item.get("score"))),
        }
        native = _extract_native(kind, item)
        attachments = list(_build_attachments(kind, item))

        source_id = registry.register(
            kind=kind,
            native_identity=native_identity,
            native=native,
            title=title,
            excerpt=excerpt,
            retrieved_by=retrieved_by,
            attachments=attachments,
        )
        if source_id is not None:
            item["_cite"] = f"[src:{source_id}]"
            item["_src_id"] = source_id
    except Exception:
        logger.warning(
            "cite_tool_output: annotation failed for kind=%s tool=%s",
            kind,
            tool_name,
            exc_info=True,
        )


# ---- per-kind helpers --------------------------------------------------


def _derive_native_identity(kind: str, item: dict) -> str | None:
    if kind == "channel_message":
        platform = item.get("platform") or "slack"
        channel_id = item.get("channel_id") or ""
        message_ts = item.get("message_ts") or item.get("timestamp") or ""
        fact_id = item.get("fact_id") or item.get("id") or "0"
        if not channel_id and not message_ts:
            return None
        return f"{platform}:{channel_id}:{message_ts}:{fact_id}"
    if kind == "wiki_page":
        return f"{item.get('channel_id', '')}:{item.get('page_type', '')}:{item.get('slug', '')}:{item.get('version', '')}"
    if kind == "qa_history":
        return str(item.get("qa_id") or item.get("id") or "")
    if kind == "uploaded_file":
        return f"{item.get('file_id', '')}:{item.get('page', '')}"
    if kind == "web_result":
        return str(item.get("url") or "")
    if kind == "graph_relationship":
        return (
            f"{item.get('subject_id', '')}:{item.get('predicate', '')}:{item.get('object_id', '')}"
        )
    if kind == "decision_record":
        return str(item.get("decision_id") or "")
    if kind == "media":
        return str(
            item.get("media_id")
            or f"{item.get('channel_id', '')}:{item.get('timestamp', '')}:{(item.get('media_urls') or [''])[0]}"
        )
    return None


def _extract_excerpt(_kind: str, item: dict) -> str:
    for key in ("text", "answer", "content", "summary", "excerpt"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _extract_title(kind: str, item: dict) -> str:
    if kind == "channel_message":
        author = item.get("author", "")
        channel = item.get("channel_name") or item.get("channel_id") or ""
        return f"{author} in #{channel}" if author or channel else "Channel message"
    if kind == "wiki_page":
        return item.get("title") or f"Wiki: {item.get('page_type', '')}"
    if kind == "qa_history":
        return item.get("question") or "Past Q&A"
    if kind == "uploaded_file":
        return item.get("filename") or "Uploaded file"
    if kind == "web_result":
        return item.get("title") or item.get("url") or "Web result"
    if kind == "media":
        return item.get("title") or "Media"
    return kind


def _extract_native(kind: str, item: dict) -> dict[str, Any]:
    """Pick the subset of fields kept on `Source.native` for rendering/resolve."""
    if kind == "channel_message":
        return {
            "platform": item.get("platform"),
            "channel_id": item.get("channel_id"),
            "channel_name": item.get("channel_name"),
            "author": item.get("author"),
            "author_id": item.get("author_id"),
            "message_ts": item.get("message_ts") or item.get("timestamp"),
            "message_id": item.get("message_id"),
            "fact_id": item.get("fact_id"),
            "workspace_domain": item.get("workspace_domain"),
            "guild_id": item.get("guild_id"),
            "timestamp": item.get("timestamp"),
        }
    if kind == "wiki_page":
        return {
            "channel_id": item.get("channel_id"),
            "page_type": item.get("page_type"),
            "slug": item.get("slug"),
            "version": item.get("version"),
        }
    if kind == "qa_history":
        return {
            "qa_id": item.get("qa_id") or item.get("id"),
            "session_id": item.get("session_id"),
            "asked_at": item.get("asked_at") or item.get("timestamp"),
            # Phase 3: transitive citations from the past answer's envelope.
            # One level deep only — never recurse.
            "prior_citations": _extract_prior_citations(item),
        }
    if kind == "uploaded_file":
        return {
            "file_id": item.get("file_id"),
            "filename": item.get("filename"),
            "mime_type": item.get("mime_type"),
            "page": item.get("page"),
        }
    if kind == "web_result":
        return {
            "url": item.get("url"),
            "site": item.get("site"),
            "published_at": item.get("published_at"),
        }
    if kind == "media":
        return {
            "platform": item.get("platform"),
            "channel_id": item.get("channel_id"),
            "message_ts": item.get("timestamp"),
            "media_type": item.get("media_type"),
        }
    return {}


def _build_attachments(kind: str, item: dict) -> Iterable[MediaAttachment]:
    if kind in ("channel_message", "media"):
        yield from _channel_attachments(item)
        return
    if kind == "web_result":
        url = item.get("url")
        if url:
            yield MediaAttachment(
                kind="link_preview",
                url=str(url),
                title=item.get("title"),
            )
        return
    if kind == "uploaded_file":
        file_url = item.get("url") or f"/files/{item.get('file_id', '')}"
        mime = item.get("mime_type") or ""
        att_kind = _mime_to_kind(mime)
        yield MediaAttachment(
            kind=att_kind,
            url=file_url,
            filename=item.get("filename"),
            mime_type=mime or None,
            byte_size=item.get("size_bytes"),
        )
        return
    # wiki_page, qa_history, graph_relationship, decision_record → no attachments
    return


def _channel_attachments(item: dict) -> Iterable[MediaAttachment]:
    media_type_field = (item.get("media_type") or "").lower()
    mapped = _media_type_to_kind(media_type_field)

    for url in item.get("media_urls") or []:
        if not url:
            continue
        yield MediaAttachment(kind=mapped, url=str(url))
    # Back-compat single-url field if no list.
    if not item.get("media_urls") and item.get("source_media_url"):
        yield MediaAttachment(kind=mapped, url=str(item.get("source_media_url")))

    link_urls = item.get("link_urls") or []
    link_titles = item.get("link_titles") or []
    for i, url in enumerate(link_urls):
        if not url:
            continue
        title = link_titles[i] if i < len(link_titles) else None
        yield MediaAttachment(kind="link_preview", url=str(url), title=title)


def _media_type_to_kind(raw: str) -> str:
    t = (raw or "").strip().lower()
    if t in ("image", "pdf", "video", "audio"):
        return t  # type: ignore[return-value]
    if t in ("doc", "document"):
        return "document"  # type: ignore[return-value]
    # Default to 'document' for unknown types; link_preview is only for URLs.
    return "document"  # type: ignore[return-value]


def _mime_to_kind(mime: str) -> str:
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"  # type: ignore[return-value]
    if m == "application/pdf":
        return "pdf"  # type: ignore[return-value]
    if m.startswith("video/"):
        return "video"  # type: ignore[return-value]
    if m.startswith("audio/"):
        return "audio"  # type: ignore[return-value]
    return "document"  # type: ignore[return-value]


_MAX_PRIOR_CITATIONS = 5


def _extract_prior_citations(item: dict) -> list[dict]:
    """Extract a trimmed list of prior sources from a QA-history entry.

    QAHistoryStore returns past entries with a `citations` field that, after
    the Phase 1 read shim, is either the legacy flat list or the envelope
    `items`. We return a small summary per source suitable for rendering a
    "Derived from" sub-row without exposing the full envelope.
    """
    raw = item.get("citations") or []
    if isinstance(raw, dict):
        # Envelope shape — prefer structured `sources` when populated,
        # else fall back to flat `items`.
        sources = raw.get("sources") or []
        if sources:
            trimmed = [
                {
                    "id": s.get("id"),
                    "kind": s.get("kind"),
                    "title": s.get("title"),
                    "author": (s.get("native") or {}).get("author"),
                    "channel": (s.get("native") or {}).get("channel_name")
                    or (s.get("native") or {}).get("channel_id"),
                    "timestamp": (s.get("native") or {}).get("timestamp"),
                }
                for s in sources[:_MAX_PRIOR_CITATIONS]
                if isinstance(s, dict)
            ]
            return trimmed
        raw = raw.get("items") or []
    if isinstance(raw, list):
        return [
            {
                "id": None,
                "kind": c.get("type") or "channel_message",
                "title": c.get("text", "")[:80],
                "author": c.get("author"),
                "channel": c.get("channel"),
                "timestamp": c.get("timestamp"),
            }
            for c in raw[:_MAX_PRIOR_CITATIONS]
            if isinstance(c, dict)
        ]
    return []


def _normalize_score(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f > 1.0:
        # Heuristic: values > 1 are on a 0-100 scale; normalize.
        f = f / 100.0
    if f < 0:
        return 0.0
    if f > 1:
        return 1.0
    return f


def _extract_query_from_args(bound: dict[str, Any]) -> str:
    """Pull the `query` argument from the bound name→value mapping."""
    for key in ("query", "topic", "topic_name"):
        v = bound.get(key)
        if isinstance(v, str) and v:
            return v
    return ""
