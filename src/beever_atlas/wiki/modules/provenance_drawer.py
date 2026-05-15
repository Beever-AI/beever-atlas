"""``provenance_drawer`` module — frontend renderer.

Surfaces the source messages each fact on the page came from. Every
page where ``fact_count >= 1`` gets this module — readers (humans
AND LLM agents reading the wiki) can drill from a synthesised claim
down to the original conversation that produced it.

Source data on each fact (populated by the orchestrator's adapter):
  - ``author_name`` — speaker name
  - ``message_ts`` — ISO timestamp
  - ``source_message_id`` — platform message id (used to dedup
    multiple facts that came from the same single message)
  - ``platform`` — slack/mattermost/discord/...
  - ``permalink`` — platform deep-link (when available)
  - ``memory_text`` — fallback snippet when the original message
    body wasn't plumbed through

Grouping: ``(platform, source_message_id)`` so multiple facts from
the same message collapse to one row. Sort by ``ts`` ASC. Cap
displayed messages at 25; the frontend uses ``total_count`` to show
"+N more" when truncated.

Renderer lives in
``web/src/components/wiki/modules/ProvenanceDrawerModule.tsx`` —
this file is purely a builder.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers

# Maximum messages we ship to the frontend. The drawer is collapsed
# by default; ~25 rows is a comfortable scroll size when expanded.
# ``total_count`` carries the full size so the frontend can render a
# "+N more" affordance.
_MAX_MESSAGES = 25
# Snippet length cap. 200 chars is two short lines on a typical
# wiki layout — enough context without burying the rest of the
# drawer.
_SNIPPET_MAX = 200


def _snippet(text: str) -> str:
    """Truncate a message body to the snippet budget, cutting at a
    word boundary when possible. Empty / whitespace-only returns
    ``""``."""
    if not text:
        return ""
    s = " ".join(str(text).split())
    if len(s) <= _SNIPPET_MAX:
        return s
    budget = s[:_SNIPPET_MAX]
    last_space = budget.rfind(" ")
    # Prefer a word boundary if it falls in the second half of the
    # budget; otherwise hard-truncate with an ellipsis.
    if last_space >= _SNIPPET_MAX // 2:
        return budget[:last_space].rstrip(" ,;:") + "…"
    return budget.rstrip() + "…"


def build_provenance_drawer_data(facts: list[Any] | None) -> dict[str, Any]:
    """Build the payload the React ProvenanceDrawerModule consumes.

    Pure function over ``facts`` — no IO, no LLM. Groups facts by
    ``(platform, source_message_id)`` so multiple facts from the same
    source message collapse into one row.

    Returns:
        {
          "label": "Source messages",
          "renderer_kind": "frontend",
          "messages": [
            {
              "ts": "2026-04-22T10:32:00Z",
              "author": "Jacky Chan",
              "platform": "mattermost",
              "channel": "tech-beever-atlas",  # if available
              "url": "https://team.votee.com/...",  # platform deep-link
              "snippet": "<= 200 chars from the original message",
              "contributed_to_facts": ["fact_id_a", "fact_id_b"]
            }
          ],
          "total_count": int
        }
    """
    if not isinstance(facts, list):
        return {
            "label": "Source messages",
            "renderer_kind": "frontend",
            "messages": [],
            "total_count": 0,
        }

    # Bucket by (platform, source_message_id). When we don't have a
    # source_message_id (older facts), fall back to the fact's id so
    # each row is at least 1 fact (no spurious collapsing).
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for f in facts:
        if not isinstance(f, dict):
            continue
        platform = str(f.get("platform") or "")
        source_msg_id = str(f.get("source_message_id") or f.get("message_id") or "")
        # Per-fact id used for the contributed_to_facts list and as
        # the unique key when source_message_id is unknown.
        fact_id = str(f.get("fact_id") or f.get("id") or "")
        key_id = source_msg_id or fact_id
        if not key_id:
            # Fact lacks both a message id and a fact id — skip rather
            # than collapse everything into a single bucket.
            continue
        key = (platform, key_id)

        ts = str(f.get("message_ts") or f.get("timestamp") or f.get("date") or "")
        author = str(f.get("author_name") or f.get("user_name") or f.get("author") or "")
        url = str(f.get("permalink") or f.get("source_url") or f.get("message_url") or "")
        channel = str(f.get("channel_name") or f.get("channel") or "")
        # Snippet — prefer the original message body when plumbed
        # through; fall back to the synthesised fact text. Either way
        # the reader gets something readable. Strip any safety
        # markers (``<untrusted>`` etc.) before truncation so the
        # tags don't leak to the user.
        body = _strip_safety_markers(
            f.get("source_message_text")
            or f.get("message_text")
            or f.get("memory_text")
            or f.get("fact")
            or f.get("text")
            or ""
        )
        snippet = _snippet(body)

        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                "ts": ts,
                "author": author,
                "platform": platform,
                "channel": channel,
                "url": url,
                "snippet": snippet,
                "contributed_to_facts": [fact_id] if fact_id else [],
            }
        else:
            # Same source message produced this additional fact —
            # append the fact_id, but keep the earlier snippet/ts/url
            # to avoid jitter from per-fact differences.
            if fact_id and fact_id not in existing["contributed_to_facts"]:
                existing["contributed_to_facts"].append(fact_id)
            # Promote a richer snippet if the new fact carries the
            # original message body and the previous one didn't.
            if not existing["snippet"] and snippet:
                existing["snippet"] = snippet
            # Promote a non-empty author/url/channel if the previous
            # entry was missing one — facts from the same message
            # sometimes have partial metadata.
            if not existing["author"] and author:
                existing["author"] = author
            if not existing["url"] and url:
                existing["url"] = url
            if not existing["channel"] and channel:
                existing["channel"] = channel

    # Stable order: chronological ASC by ts (lexicographic ISO order),
    # then by platform/key for tie-break determinism.
    messages: list[dict[str, Any]] = list(grouped.values())
    messages.sort(key=lambda m: (m.get("ts") or "", m.get("platform") or ""))

    total_count = len(messages)
    if total_count > _MAX_MESSAGES:
        messages = messages[:_MAX_MESSAGES]

    return {
        "label": "Source messages",
        "renderer_kind": "frontend",
        "messages": messages,
        "total_count": total_count,
    }
