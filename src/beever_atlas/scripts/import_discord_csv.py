"""Import a DiscordChatExporter CSV into the dry-run cache format.

Usage:
    uv run python -m beever_atlas.scripts.import_discord_csv <path_to_csv> [--channel-id ID] [--limit N]

Then test with:
    uv run python -m beever_atlas.scripts.dry_run <channel_id> --cached --limit 50
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


def parse_csv(csv_path: Path, channel_id: str, limit: int = 0) -> list[dict]:
    """Convert DiscordChatExporter CSV rows to NormalizedMessage dicts."""
    messages = []
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if limit and i >= limit:
                break

            content = row.get("Content", "").strip()
            # Skip empty, bot system messages, and pin notifications
            if not content or content in ("Pinned a message.", "This message was deleted."):
                continue

            author_id = row.get("AuthorID", "")
            author = row.get("Author", "unknown")
            date_str = row.get("Date", "")
            attachments_raw = row.get("Attachments", "")
            reactions_raw = row.get("Reactions", "")

            # Parse timestamp
            try:
                ts = datetime.fromisoformat(date_str).astimezone(timezone.utc)
            except ValueError:
                ts = datetime.now(timezone.utc)

            # Parse attachments (comma-separated URLs)
            attachments = []
            if attachments_raw:
                for url in attachments_raw.split(","):
                    url = url.strip()
                    if url:
                        attachments.append({"url": url, "type": "file"})

            # Parse reactions (e.g. "👍 - 2, ❤️ - 1")
            reactions = []
            if reactions_raw:
                for part in reactions_raw.split(","):
                    part = part.strip()
                    m = re.match(r"(.+)\s*-\s*(\d+)", part)
                    if m:
                        reactions.append({"name": m.group(1).strip(), "count": int(m.group(2))})

            msg = {
                "content": content,
                "author": author_id,
                "author_name": author,
                "author_image": "",
                "platform": "discord",
                "channel_id": channel_id,
                "channel_name": channel_id,
                "message_id": str(uuid.uuid4()),
                "timestamp": ts.isoformat(),
                "thread_id": None,
                "attachments": attachments,
                "reactions": reactions,
                "reply_count": 0,
                "raw_metadata": {"author_id": author_id, "date": date_str},
                # dry_run.py also looks for these keys in preprocessed messages
                "text": content,
                "username": author,
                "ts": ts.timestamp(),
            }
            messages.append(msg)

    return messages


def main() -> None:
    parser = argparse.ArgumentParser(description="Import DiscordChatExporter CSV to dry-run cache")
    parser.add_argument("csv_path", help="Path to the exported CSV file")
    parser.add_argument("--channel-id", help="Override channel ID (default: extracted from filename)")
    parser.add_argument("--limit", type=int, default=0, help="Max messages to import (0 = all)")
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        print(f"Error: file not found: {csv_path}")
        return

    # Extract channel ID from filename bracket notation, e.g. [440061296017408010]
    channel_id = args.channel_id
    if not channel_id:
        m = re.search(r"\[(\d+)\]", csv_path.stem)
        channel_id = m.group(1) if m else csv_path.stem

    print(f"Channel ID: {channel_id}")
    print(f"Parsing {csv_path.name} ...")

    messages = parse_csv(csv_path, channel_id, limit=args.limit)
    print(f"Parsed {len(messages)} messages")

    cache_dir = Path(".omc/cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_file = cache_dir / f"messages-{channel_id}.json"
    out_file.write_text(json.dumps(messages, ensure_ascii=False, indent=2))
    print(f"Saved to {out_file}")
    print()
    print("Now run:")
    print(f"  uv run python -m beever_atlas.scripts.dry_run {channel_id} --cached --limit 50")
    print(f"  uv run python -m beever_atlas.scripts.dry_run {channel_id} --cached --facts-only")
    print(f"  uv run python -m beever_atlas.scripts.dry_run {channel_id} --cached --batch-api")


if __name__ == "__main__":
    main()
