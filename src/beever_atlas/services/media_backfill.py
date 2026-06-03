"""Backfill durable media blobs for already-ingested channels.

Channel media is referenced everywhere by its platform CDN URL, but those URLs
rot (Discord signed URLs expire ~24h; Slack/Mattermost/Teams URLs need a live
bot token forever). The durable :class:`~beever_atlas.stores.media_blob_store.MediaBlobStore`
fixes new ingestion going forward; this service re-fetches and stores media for
messages that were ingested *before* the blob store shipped.

It is shared by the CLI script
(``scripts/backfill_channel_media.py``) and the admin endpoint
(``POST /api/admin/channels/{id}/backfill-media``).

Design
------
* Iterates ``channel_messages`` oldest-first via an ``_id`` cursor (the same
  resumable batching the gold-standard ``migrate_imported_messages_*`` script
  uses), filtering to rows that carry attachments.
* Reuses :meth:`MediaProcessor._download_file` for the byte fetch — the bridge
  proxy, retry-on-429, HTML-login detection, and size cap all live there; we
  never duplicate download logic.
* Resolves each channel's ``connection_id`` once (cached per channel) so Slack
  multi-workspace fetches route through the right bot token. The bridge
  ``/bridge/files`` endpoint needs ``connection_id`` to avoid returning an HTML
  login page for Slack. When the mapping is unresolvable we fall back to
  ``connection_id=None`` (bridge auto-detect).
* Idempotent: a ref that already exists (``blob_store.has_ref``) is counted as
  ``already_stored`` and skipped, so re-runs are cheap. Telegram URLs are never
  stored (their path embeds the bot token) and counted ``skipped_telegram``.
* Resumable: the last processed ``_id`` plus running counts are persisted to the
  ``media_backfill_state`` collection every batch (key
  ``media_backfill_state:<channel_id or 'all'>``); on start the loop resumes from
  it unless ``reset=True``.
* Gentle on the bridge: messages within a batch are processed with a bounded
  ``asyncio.Semaphore(4)``; attachments within a message run sequentially.
  Slack file fetches are serialized at ~200ms on the bridge side, so we avoid
  retry-storming.

dry_run
-------
``dry_run=True`` never downloads. Each fetch candidate (a non-Telegram
attachment with a URL that is not already stored) is counted under ``stored``
("would store") and reported. No bytes move; the resume state is not written.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from beever_atlas.services.media_processor import OVERSIZE, MediaProcessor
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

# Telegram file URLs carry the bot token in the path — never store them as a
# ref (mirrors MediaBlobStore.normalize_url_key's Telegram guard).
_TELEGRAM_HOST = "api.telegram.org"

# State collection + key prefix for the resume cursor (mirrors the
# ``migration_state`` doc convention of the migrate-imported-messages script).
_STATE_COLLECTION = "media_backfill_state"

# Bounded message concurrency within a batch. Kept deliberately low because the
# bridge serializes Slack file fetches at ~200ms; a higher fan-out just queues
# behind that and risks retry-storming the proxy.
_MESSAGE_CONCURRENCY = 4


def _state_id(channel_id: str | None) -> str:
    """Resume-doc ``_id`` for a backfill scope (one channel, or all)."""
    return f"{_STATE_COLLECTION}:{channel_id or 'all'}"


def _is_telegram_url(url: str) -> bool:
    """True when ``url``'s host is Telegram's file API (token-bearing path)."""
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower() == _TELEGRAM_HOST
    except Exception:
        return False


@dataclass
class BackfillReport:
    """Per-run counters for a media backfill.

    Counts are tallied per outcome so an operator can tell expired/auth
    failures (``download_failed``) apart from oversized files (``too_large``)
    and link-less attachments (``no_url``). ``by_platform`` mirrors the same
    counters keyed by ``source_id`` so a mixed-source channel can be diagnosed.
    """

    channel_id: str | None = None
    dry_run: bool = False
    messages_scanned: int = 0
    stored: int = 0
    already_stored: int = 0
    no_url: int = 0
    download_failed: int = 0
    too_large: int = 0
    skipped_telegram: int = 0
    errors: list[str] = field(default_factory=list)
    by_platform: dict[str, dict[str, int]] = field(default_factory=dict)

    def _bump_platform(self, source_id: str, key: str) -> None:
        """Increment ``by_platform[source_id][key]`` (zero-filled on first hit)."""
        bucket = self.by_platform.setdefault(
            source_id or "unknown",
            {
                "stored": 0,
                "already_stored": 0,
                "no_url": 0,
                "download_failed": 0,
                "too_large": 0,
                "skipped_telegram": 0,
            },
        )
        bucket[key] = bucket.get(key, 0) + 1

    def record(self, source_id: str, key: str) -> None:
        """Increment a per-attachment outcome counter on both totals + platform."""
        setattr(self, key, getattr(self, key) + 1)
        self._bump_platform(source_id, key)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable summary for the CLI log + admin response body."""
        return {
            "channel_id": self.channel_id,
            "dry_run": self.dry_run,
            "messages_scanned": self.messages_scanned,
            "stored": self.stored,
            "already_stored": self.already_stored,
            "no_url": self.no_url,
            "download_failed": self.download_failed,
            "too_large": self.too_large,
            "skipped_telegram": self.skipped_telegram,
            "errors": self.errors,
            "by_platform": self.by_platform,
        }


class _ConnectionResolver:
    """Resolve a channel's ``connection_id``, cached per channel.

    The mapping is ``PlatformConnection.selected_channels`` — a channel belongs
    to the (first, id-sorted for determinism) connected connection whose
    ``selected_channels`` lists it. This mirrors the authoritative lookup the
    admin reset path uses (``api/admin.py`` reset → ``in_selected``) and the
    channel-list builder (``api/channels.py``). When no connection claims the
    channel we return ``None`` so the bridge auto-detects.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}

    async def resolve(self, channel_id: str) -> str | None:
        if channel_id in self._cache:
            return self._cache[channel_id]
        connection_id: str | None = None
        try:
            connections = await get_stores().platform.list_connections()
            claiming = [
                c
                for c in connections
                if channel_id in (getattr(c, "selected_channels", None) or [])
            ]
            if claiming:
                connection_id = sorted(claiming, key=lambda c: c.id)[0].id
        except Exception as exc:  # noqa: BLE001 — fall back to bridge auto-detect
            logger.warning(
                "MediaBackfill: connection resolve failed channel=%s error=%s "
                "(falling back to auto-detect)",
                channel_id,
                exc,
            )
        self._cache[channel_id] = connection_id
        return connection_id


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


async def _load_resume_id(channel_id: str | None) -> Any | None:
    """Return the last processed ``channel_messages._id`` if a prior run wrote one."""
    state_col = get_stores().mongodb.db[_STATE_COLLECTION]
    doc = await state_col.find_one({"_id": _state_id(channel_id)})
    if doc is None:
        return None
    return doc.get("last_processed_id")


async def _save_resume_state(
    channel_id: str | None,
    last_id: Any,
    report: BackfillReport,
) -> None:
    """Persist the resume cursor + running counters every batch."""
    state_col = get_stores().mongodb.db[_STATE_COLLECTION]
    now = datetime.now(tz=UTC)
    await state_col.update_one(
        {"_id": _state_id(channel_id)},
        {
            "$set": {
                "last_processed_id": last_id,
                "counts": report.to_dict(),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def _clear_resume_state(channel_id: str | None) -> None:
    """Drop the resume doc so the next run starts from the oldest message."""
    state_col = get_stores().mongodb.db[_STATE_COLLECTION]
    await state_col.delete_one({"_id": _state_id(channel_id)})


# ---------------------------------------------------------------------------
# Per-message processing
# ---------------------------------------------------------------------------


async def _process_message(
    *,
    doc: dict[str, Any],
    processor: MediaProcessor,
    connection_id: str | None,
    dry_run: bool,
    report: BackfillReport,
) -> None:
    """Store (or count) every attachment on one message.

    Attachments are processed sequentially — the bridge serializes Slack file
    fetches, so per-message parallelism buys nothing and risks rate limits.
    """
    channel_id = str(doc.get("channel_id") or "")
    source_id = str(doc.get("source_id") or "")
    message_id = str(doc.get("message_id") or "")
    attachments = doc.get("attachments") or []

    for att in attachments:
        if not isinstance(att, dict):
            continue
        url = att.get("url") or att.get("url_private") or ""
        name = att.get("name") or att.get("title") or "file"
        mime = att.get("mimetype") or ""

        if not url:
            report.record(source_id, "no_url")
            continue
        if _is_telegram_url(url):
            report.record(source_id, "skipped_telegram")
            continue

        try:
            if await get_stores().media_blob_store.has_ref(url, channel_id):
                report.record(source_id, "already_stored")
                continue
        except Exception as exc:  # noqa: BLE001 — never abort the batch on a probe error
            logger.warning(
                "MediaBackfill: has_ref probe failed channel=%s url=%.80s error=%s",
                channel_id,
                url,
                exc,
            )

        if dry_run:
            # Count the fetch candidate as "would store" without downloading.
            report.record(source_id, "stored")
            continue

        data = await processor._download_file(
            url, connection_id=connection_id, return_oversize_sentinel=True
        )
        if data is OVERSIZE:
            # Rejected solely by the size cap — kept distinct from auth/network
            # failures so the operator report can surface oversized files.
            report.record(source_id, "too_large")
            continue
        if not isinstance(data, bytes) or not data:
            # ``None`` (or empty) covers expired/auth failures, HTML login
            # pages, and network errors — all reported as a failed download.
            report.record(source_id, "download_failed")
            continue

        try:
            await get_stores().media_blob_store.save_blob(
                content=data,
                mime_type=mime,
                filename=name,
                channel_id=channel_id,
                source_id=source_id,
                message_id=message_id,
                platform_url=url,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, keep scanning
            logger.warning(
                "MediaBackfill: save_blob failed channel=%s url=%.80s error=%s",
                channel_id,
                url,
                exc,
            )
            report.errors.append(f"save_blob failed for {name}")
            continue

        report.record(source_id, "stored")


# ---------------------------------------------------------------------------
# Main backfill loop
# ---------------------------------------------------------------------------


async def backfill_channel_media(
    *,
    channel_id: str | None = None,
    dry_run: bool = False,
    batch_size: int = 100,
    max_messages: int | None = None,
    reset: bool = False,
) -> BackfillReport:
    """Re-fetch and durably store media for already-ingested messages.

    Args:
        channel_id: Restrict to one channel. ``None`` backfills every channel.
        dry_run: Count fetch candidates without downloading or writing.
        batch_size: ``channel_messages`` rows per Mongo batch.
        max_messages: Optional cap on messages scanned (testing / staged runs).
        reset: Clear the resume cursor before starting (re-scan from oldest).

    Returns:
        A :class:`BackfillReport` with per-outcome + per-platform counts.

    Resumability uses an ``_id`` cursor — Mongo ObjectIds are monotonically
    increasing, so ``$gt: last_id`` advances reliably without skipping rows.
    """
    report = BackfillReport(channel_id=channel_id, dry_run=dry_run)
    stores = get_stores()
    messages = stores.mongodb._channel_messages  # type: ignore[attr-defined]

    processor = MediaProcessor()
    resolver = _ConnectionResolver()

    if reset and not dry_run:
        await _clear_resume_state(channel_id)

    # dry_run never persists/reads resume state — it is a pure count.
    resume_id = None if dry_run else await _load_resume_id(channel_id)
    if resume_id is not None:
        logger.info(json.dumps({"event": "media_backfill_resume", "from_id": str(resume_id)}))

    try:
        while True:
            # Only rows that actually carry attachments — ``{"$ne": []}`` plus an
            # existence check skips the (large) attachment-free majority.
            query: dict[str, Any] = {
                "attachments": {"$exists": True, "$ne": []},
            }
            if channel_id is not None:
                query["channel_id"] = channel_id
            if resume_id is not None:
                query["_id"] = {"$gt": resume_id}

            limit = batch_size
            if max_messages is not None:
                remaining = max_messages - report.messages_scanned
                if remaining <= 0:
                    break
                limit = min(batch_size, remaining)

            cursor = messages.find(query).sort("_id", 1).limit(limit)
            batch: list[dict[str, Any]] = []
            async for row in cursor:
                batch.append(row)

            if not batch:
                break

            # Resolve each batch member's connection_id (cached per channel) and
            # process messages with bounded concurrency.
            sem = asyncio.Semaphore(_MESSAGE_CONCURRENCY)

            async def _run(doc: dict[str, Any]) -> None:
                cid = str(doc.get("channel_id") or "")
                conn_id = await resolver.resolve(cid) if cid else None
                async with sem:
                    await _process_message(
                        doc=doc,
                        processor=processor,
                        connection_id=conn_id,
                        dry_run=dry_run,
                        report=report,
                    )

            await asyncio.gather(*[_run(doc) for doc in batch])
            report.messages_scanned += len(batch)

            resume_id = batch[-1]["_id"]
            if not dry_run:
                await _save_resume_state(channel_id, resume_id, report)

            logger.info(
                json.dumps(
                    {
                        "event": "media_backfill_progress",
                        "channel_id": channel_id,
                        "messages_scanned": report.messages_scanned,
                        "stored": report.stored,
                        "already_stored": report.already_stored,
                        "download_failed": report.download_failed,
                        "skipped_telegram": report.skipped_telegram,
                    }
                )
            )

            if len(batch) < limit:
                # Short read — nothing more to fetch.
                break
    finally:
        await processor.close()

    logger.info(json.dumps({"event": "media_backfill_complete", **report.to_dict()}))
    return report
