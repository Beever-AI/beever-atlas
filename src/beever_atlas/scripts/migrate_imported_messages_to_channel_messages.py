"""One-shot migration: ``imported_messages`` → ``channel_messages``.

Reads every row from the legacy ``imported_messages`` collection (file imports
written by ``api/imports.py:commit_import``), maps each into a
:class:`beever_atlas.models.persistence.ChannelMessage` with
``source_id="file"``, and writes via
:meth:`beever_atlas.stores.mongodb_store.MongoDBStore.upsert_channel_messages`.

Idempotency
-----------
``channel_messages`` carries a compound unique index on
``(source_id, channel_id, message_id)``. Re-running this script after a
successful run is a no-op for inserted rows; only the ``$set`` mutable
fields (content, channel_name, attachments, etc.) refresh, and the worker
state machine fields (``extraction_status``, ``attempt_count``,
``next_attempt_at``, ``last_error``, ``created_at``) are guarded by
``$setOnInsert`` so a re-run does not reset extraction progress.

Resumability
------------
The script tracks the last processed ``imported_messages._id`` in a small
state document in the ``migration_state`` collection under the key
``imported_messages_to_channel_messages``. On restart it resumes from
``{"_id": {"$gt": <last_processed_id>}}`` so a Ctrl+C halfway through is safe.

extraction_status default
-------------------------
**Pragmatic shortcut**: ALL migrated rows are written with
``extraction_status="pending"`` so the ExtractionWorker re-extracts them.
Re-extraction of already-done messages is wasteful but not corrupting:

  * The ``$setOnInsert`` semantics keep the worker's per-message state
    immutable on existing rows once it claims them.
  * The ExtractionWorker uses a content-derived deterministic fact ID so
    duplicate extraction yields the same Weaviate row id (idempotent write).

The alternative — querying Neo4j ``MessageNode`` and Weaviate by message_id
to derive ``done`` vs ``pending`` per row — is significantly more expensive
and complicates the migration. Documented choice; can be revisited if
soak observes a meaningful re-extraction cost.

Usage
-----
::

    # Dry run: count what would be migrated, log samples, no writes.
    uv run python -m beever_atlas.scripts.migrate_imported_messages_to_channel_messages --dry-run

    # Migrate everything in 500-row batches.
    uv run python -m beever_atlas.scripts.migrate_imported_messages_to_channel_messages

    # Migrate one channel only (testing).
    uv run python -m beever_atlas.scripts.migrate_imported_messages_to_channel_messages --source-channel-id abc-123

    # Smaller batches to bound memory pressure for huge collections.
    uv run python -m beever_atlas.scripts.migrate_imported_messages_to_channel_messages --batch-size 100

The script logs structured JSON progress every batch and a final
``migration_complete`` line on success.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Load .env so MONGODB_URI is honoured when invoked via `uv run`.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from beever_atlas.infra.config import get_settings  # noqa: E402
from beever_atlas.models.persistence import ChannelMessage  # noqa: E402
from beever_atlas.stores.mongodb_store import MongoDBStore  # noqa: E402

logger = logging.getLogger(__name__)


MIGRATION_STATE_KEY = "imported_messages_to_channel_messages"


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _coerce_timestamp(raw: Any) -> datetime:
    """Best-effort coerce a stored timestamp into a tz-aware ``datetime``.

    ``imported_messages`` rows can carry either a Mongo BSON ``datetime``
    (already a Python datetime via motor) or an ISO-8601 string under
    ``timestamp_iso``. Falls back to ``now`` so a malformed row doesn't
    break the migration mid-batch — the original row stays in
    ``imported_messages`` if anything goes wrong.
    """
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=UTC)
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            pass
    return datetime.now(tz=UTC)


def _row_to_channel_message(row: dict[str, Any]) -> ChannelMessage | None:
    """Convert one ``imported_messages`` row into a ``ChannelMessage``.

    Returns ``None`` for rows without the minimum identity fields
    (channel_id + message_id) — they are skipped rather than failing the
    whole batch.
    """
    channel_id = row.get("channel_id")
    message_id = row.get("message_id")
    if not channel_id or not message_id:
        return None

    raw_ts = row.get("timestamp") or row.get("timestamp_iso")
    timestamp = _coerce_timestamp(raw_ts)

    try:
        return ChannelMessage(
            source_id="file",
            channel_id=str(channel_id),
            message_id=str(message_id),
            channel_name=str(row.get("channel_name") or ""),
            timestamp=timestamp,
            author=str(row.get("author") or ""),
            author_name=str(row.get("author_name") or ""),
            author_image=str(row.get("author_image") or ""),
            content=str(row.get("content") or ""),
            thread_id=row.get("thread_id"),
            attachments=list(row.get("attachments") or []),
            reactions=list(row.get("reactions") or []),
            reply_count=int(row.get("reply_count") or 0),
            # Pragmatic default — see module docstring.
            extraction_status="pending",
        )
    except Exception as exc:  # noqa: BLE001 — best-effort conversion
        logger.warning(
            "migrate: row→ChannelMessage conversion failed channel_id=%s "
            "message_id=%s exc=%s: %.200s",
            channel_id,
            message_id,
            type(exc).__name__,
            str(exc),
        )
        return None


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


async def _load_resume_id(store: MongoDBStore) -> Any | None:
    """Return the last processed ``imported_messages._id`` if a prior run wrote one."""
    state_col = store.db["migration_state"]
    doc = await state_col.find_one({"_id": MIGRATION_STATE_KEY})
    if doc is None:
        return None
    return doc.get("last_processed_id")


async def _save_resume_id(
    store: MongoDBStore,
    last_id: Any,
    migrated: int,
    skipped: int,
) -> None:
    """Persist the resume cursor + running counters."""
    state_col = store.db["migration_state"]
    await state_col.update_one(
        {"_id": MIGRATION_STATE_KEY},
        {
            "$set": {
                "last_processed_id": last_id,
                "migrated": migrated,
                "skipped": skipped,
                "updated_at": datetime.now(tz=UTC),
            },
            "$setOnInsert": {"created_at": datetime.now(tz=UTC)},
        },
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Main migration loop
# ---------------------------------------------------------------------------


async def migrate(
    *,
    dry_run: bool = False,
    batch_size: int = 500,
    source_channel_id: str | None = None,
) -> dict[str, Any]:
    """Run the migration. Returns a summary dict with counts + duration.

    Uses an ``_id`` cursor for resumability — Mongo ObjectIds are
    monotonically increasing within a server, so ``$gt: last_id`` reliably
    advances through the collection without skipping rows that were
    inserted at the tail end of the source collection mid-migration.
    """
    settings = get_settings()
    store = MongoDBStore(uri=settings.mongodb_uri)

    started_at = time.monotonic()
    migrated = 0
    skipped = 0
    total_seen = 0

    # Cancellation flag so SIGINT / SIGTERM let us flush state and exit cleanly.
    stop = asyncio.Event()

    def _on_signal(*_args: Any) -> None:  # pragma: no cover — interactive only
        logger.info(
            json.dumps(
                {
                    "event": "migration_interrupted",
                    "migrated": migrated,
                    "skipped": skipped,
                    "total_seen": total_seen,
                }
            )
        )
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, ValueError):
            # Windows or non-main thread — fall back to default behaviour.
            pass

    try:
        imported = store.db["imported_messages"]

        resume_id = None if dry_run else await _load_resume_id(store)
        if resume_id is not None:
            logger.info(
                json.dumps(
                    {
                        "event": "migration_resume",
                        "from_id": str(resume_id),
                    }
                )
            )

        while not stop.is_set():
            query: dict[str, Any] = {}
            if source_channel_id is not None:
                query["channel_id"] = source_channel_id
            if resume_id is not None:
                query["_id"] = {"$gt": resume_id}

            cursor = imported.find(query).sort("_id", 1).limit(batch_size)
            batch_rows: list[dict[str, Any]] = []
            async for doc in cursor:
                batch_rows.append(doc)

            if not batch_rows:
                break

            channel_messages: list[ChannelMessage] = []
            for row in batch_rows:
                total_seen += 1
                cm = _row_to_channel_message(row)
                if cm is None:
                    skipped += 1
                    continue
                channel_messages.append(cm)

            if dry_run:
                # Dry-run: log a sample (first row of the very first non-empty batch)
                # so the operator can sanity-check the field mapping before flipping
                # off the flag.
                if batch_rows and migrated == 0 and channel_messages:
                    sample_in = {k: v for k, v in batch_rows[0].items() if k not in ("_id",)}
                    sample_out = channel_messages[0].model_dump(mode="json")
                    logger.info(
                        json.dumps(
                            {
                                "event": "migration_sample",
                                "input_row": _stringify_dates(sample_in),
                                "output_row": sample_out,
                            }
                        )
                    )
                migrated += len(channel_messages)
            else:
                if channel_messages:
                    result = await store.upsert_channel_messages(channel_messages)
                    migrated += len(channel_messages)
                    logger.debug(
                        "migrate: batch upsert inserted=%s modified=%s matched=%s",
                        result["inserted"],
                        result["modified"],
                        result["matched"],
                    )

            resume_id = batch_rows[-1]["_id"]

            if not dry_run:
                await _save_resume_id(store, resume_id, migrated, skipped)

            logger.info(
                json.dumps(
                    {
                        "event": "migration_progress",
                        "migrated": migrated,
                        "skipped": skipped,
                        "total_seen": total_seen,
                    }
                )
            )

            if len(batch_rows) < batch_size:
                # Last batch was a short read — nothing more to fetch.
                break
    finally:
        await store.shutdown()

    duration = round(time.monotonic() - started_at, 3)
    summary = {
        "event": "migration_complete",
        "migrated": migrated,
        "skipped": skipped,
        "total_seen": total_seen,
        "duration_seconds": duration,
        "dry_run": dry_run,
    }
    logger.info(json.dumps(summary))
    return summary


def _stringify_dates(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce datetimes inside a row dict to ISO strings for JSON-friendly logging."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, list):
            out[k] = [_stringify_dates(item) if isinstance(item, dict) else item for item in v]
        elif isinstance(v, dict):
            out[k] = _stringify_dates(v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_imported_messages_to_channel_messages",
        description=(
            "Migrate file-import rows from the legacy `imported_messages` collection "
            "to the unified `channel_messages` collection (source_id='file')."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows that would be migrated and log a sample, but do not write.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows per Mongo batch (default: 500). Lower for memory-constrained hosts.",
    )
    parser.add_argument(
        "--source-channel-id",
        default=None,
        help="Migrate only this channel_id (useful for staged rollouts / testing).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    summary = asyncio.run(
        migrate(
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            source_channel_id=args.source_channel_id,
        )
    )
    # Exit 0 unconditionally — the script is read-only on no-data and writes
    # are idempotent. Errors raise and surface a non-zero exit naturally.
    return 0 if summary else 1


if __name__ == "__main__":
    sys.exit(main())
