"""Kind-dispatched permalink resolver.

Translates `Source.native` payloads into deep links. Per the Phase 1
design: never throws, returns None on any missing metadata, logs at most
once per source id per turn.

This resolver does not read from the database directly — all workspace /
guild / team metadata needed by URL templates must already be present in
`Source.native` when the tool decorator registers the source. The
decorator is responsible for looking up platform metadata and stashing
it there. This keeps the resolver pure and trivially unit-testable.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from beever_atlas.agents.citations.types import Source

logger = logging.getLogger(__name__)

_LOGGED_NULLS: set[str] = set()  # source_ids we've already warned about


def _warn_once(source_id: str, reason: str) -> None:
    if source_id in _LOGGED_NULLS:
        return
    _LOGGED_NULLS.add(source_id)
    logger.warning("permalink null for source=%s: %s", source_id, reason)


def _slack_ts_path(ts: str) -> str | None:
    """Convert a Slack-style timestamp '1712500000.001100' → 'p1712500000001100'."""
    if not ts:
        return None
    cleaned = str(ts).strip()
    if not re.match(r"^\d+(\.\d+)?$", cleaned):
        return None
    return "p" + cleaned.replace(".", "").ljust(16, "0")


class PermalinkResolver:
    """Pure resolver keyed on kind. Safe to share across turns."""

    def resolve(self, source: Source) -> str | None:
        try:
            fn = getattr(self, f"_resolve_{source.kind}", None)
            if fn is None:
                _warn_once(source.id, f"no resolver for kind={source.kind}")
                return None
            return fn(source)
        except Exception:
            logger.warning("resolver threw for source=%s", source.id, exc_info=True)
            return None

    # ---- kind dispatchers ---------------------------------------------

    def _resolve_channel_message(self, source: Source) -> str | None:
        native = source.native
        platform = (native.get("platform") or "").lower()
        channel_id = native.get("channel_id")
        message_ts = native.get("message_ts")

        if not platform:
            _warn_once(source.id, "missing platform on channel_message")
            return None
        if not channel_id:
            _warn_once(source.id, "missing channel_id on channel_message")
            return None

        if platform == "slack":
            workspace = native.get("workspace_domain") or native.get("workspace")
            ts_path = _slack_ts_path(str(message_ts or ""))
            if not workspace or not ts_path:
                _warn_once(source.id, "slack missing workspace or ts")
                return None
            return f"https://{workspace}.slack.com/archives/{channel_id}/{ts_path}"

        if platform == "discord":
            guild_id = native.get("guild_id")
            message_id = native.get("message_id") or message_ts
            if not guild_id or not message_id:
                _warn_once(source.id, "discord missing guild_id or message_id")
                return None
            return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

        if platform == "teams":
            message_id = native.get("message_id") or message_ts
            if not message_id:
                _warn_once(source.id, "teams missing message_id")
                return None
            return f"https://teams.microsoft.com/l/message/{channel_id}/{message_id}"

        if platform == "file":
            # Imported files have no native platform URL; fall back to internal route.
            file_id = native.get("file_id")
            if file_id:
                return f"/files/{file_id}"
            return None

        _warn_once(source.id, f"unknown platform={platform}")
        return None

    def _resolve_wiki_page(self, source: Source) -> str | None:
        n = source.native
        channel_id = n.get("channel_id")
        page_type = n.get("page_type") or n.get("slug")
        if not channel_id or not page_type:
            _warn_once(source.id, "wiki_page missing channel_id or page_type")
            return None
        slug = n.get("slug")
        anchor = f"#{slug}" if slug and slug != page_type else ""
        return f"/channel/{channel_id}/wiki/{page_type}{anchor}"

    def _resolve_qa_history(self, source: Source) -> str | None:
        n = source.native
        qa_id = n.get("qa_id")
        session = n.get("session_id")
        if not qa_id:
            _warn_once(source.id, "qa_history missing qa_id")
            return None
        if session:
            return f"/ask?session={session}#qa-{qa_id}"
        return f"/ask#qa-{qa_id}"

    def _resolve_uploaded_file(self, source: Source) -> str | None:
        file_id = source.native.get("file_id")
        if not file_id:
            _warn_once(source.id, "uploaded_file missing file_id")
            return None
        return f"/files/{file_id}"

    def _resolve_web_result(self, source: Source) -> str | None:
        url = source.native.get("url")
        if not url or not re.match(r"^https?://", str(url), re.IGNORECASE):
            _warn_once(source.id, "web_result missing or invalid url")
            return None
        return str(url)

    def _resolve_media(self, source: Source) -> str | None:
        # Media citations reuse the underlying channel_message permalink
        # when available; attachments carry the direct URL independently.
        return self._resolve_channel_message(source) or None

    def _resolve_graph_relationship(self, _source: Source) -> str | None:
        return None  # no UI route yet — Phase 3

    def _resolve_decision_record(self, _source: Source) -> str | None:
        return None  # no UI route yet — Phase 3


# Module-level instance for convenience
default_resolver = PermalinkResolver()


def reset_warn_cache() -> None:
    """Used by tests to clear the warn-once cache between cases."""
    _LOGGED_NULLS.clear()


# Avoid an unused-import warning while keeping the typing clear.
_ = Any
