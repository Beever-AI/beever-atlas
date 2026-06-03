"""Backfill durable media blobs for already-ingested channels.

Re-fetches every channel-media attachment referenced by ``channel_messages``
and stores the raw bytes in the durable ``channel_media`` GridFS bucket so the
read-through proxy can serve them after the platform CDN URL rots. New
ingestion already persists media at extraction time; this one-shot script
covers messages ingested *before* the blob store shipped.

The heavy lifting lives in
:func:`beever_atlas.services.media_backfill.backfill_channel_media` (shared with
the ``POST /api/admin/channels/{id}/backfill-media`` endpoint). This module is a
thin CLI wrapper: it loads ``.env``, initializes the store singleton, parses
flags, and prints a structured JSON summary.

Resumability
------------
The backfill tracks the last processed ``channel_messages._id`` in a small state
document in the ``media_backfill_state`` collection (one per scope —
``media_backfill_state:<channel_id or 'all'>``). On restart it resumes from
``{"_id": {"$gt": <last_processed_id>}}`` so a Ctrl+C halfway through is safe.
Pass ``--reset`` to clear the cursor and re-scan from the oldest message.

Idempotency
-----------
An attachment whose ref already exists is skipped (``already_stored``), so a
completed run re-executed is a cheap no-op. Telegram URLs are never stored
(their path embeds the bot token).

Usage
-----
::

    # Dry run: count fetch candidates, no downloads, no writes.
    uv run python -m beever_atlas.scripts.backfill_channel_media --dry-run

    # Backfill every channel in 100-row batches.
    uv run python -m beever_atlas.scripts.backfill_channel_media

    # One channel only (staged rollout / testing).
    uv run python -m beever_atlas.scripts.backfill_channel_media --channel-id abc-123

    # Restart from the oldest message, ignoring any saved cursor.
    uv run python -m beever_atlas.scripts.backfill_channel_media --reset

The script logs structured JSON progress every batch and a final
``media_backfill_complete`` line on success.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Load .env so MONGODB_URI / BRIDGE_URL are honoured when invoked via `uv run`.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from beever_atlas.infra.config import get_settings  # noqa: E402
from beever_atlas.services.media_backfill import backfill_channel_media  # noqa: E402
from beever_atlas.stores import StoreClients, init_stores  # noqa: E402

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_channel_media",
        description=(
            "Re-fetch and durably store channel media (Slack/Discord/Mattermost/"
            "Teams images, PDFs, videos) for already-ingested messages so the "
            "read-through proxy can serve them after the platform URL rots."
        ),
    )
    parser.add_argument(
        "--channel-id",
        default=None,
        help="Backfill only this channel_id (useful for staged rollouts / testing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count fetch candidates without downloading or writing anything.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="channel_messages rows per Mongo batch (default: 100).",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=None,
        help="Cap on the number of messages scanned (testing / staged runs).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear the saved resume cursor and re-scan from the oldest message.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


async def _run(args: argparse.Namespace) -> dict:
    """Initialize stores, run the backfill, and return its summary dict."""
    stores = StoreClients.from_settings(get_settings())
    await stores.startup()
    init_stores(stores)
    try:
        report = await backfill_channel_media(
            channel_id=args.channel_id,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            max_messages=args.max_messages,
            reset=args.reset,
        )
        return report.to_dict()
    finally:
        await stores.shutdown()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    summary = asyncio.run(_run(args))
    # Exit 0 unconditionally — the backfill is best-effort and idempotent;
    # per-attachment failures are reported in ``errors`` / the counters, not as
    # a process failure. Unexpected exceptions raise and surface non-zero.
    logger.info(json.dumps({"event": "media_backfill_summary", **summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
