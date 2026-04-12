"""Import a DiscordChatExporter CSV into the dry-run cache format.

Delegates parsing to ``services.file_importer`` so other export formats
work via the same code path (this script just locks in the
DiscordChatExporter preset for backwards compatibility with the cache
layout expected by ``ingest_from_csv.py`` and ``dry_run.py``).

Usage:
    uv run python -m beever_atlas.scripts.import_discord_csv <path_to_csv> \
        [--channel-id ID] [--limit N]

Then:
    uv run python -m beever_atlas.scripts.dry_run <channel_id> --cached --limit 50
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict
from pathlib import Path

from beever_atlas.services.file_importer import (
    ParseOptions,
    detect_preset,
    parse_file,
    read_headers_and_samples,
)


def _build_cache_entry(msg, channel_id: str) -> dict:
    """Project a NormalizedMessage into the legacy cache schema.

    ``dry_run.py`` and ``ingest_from_csv.py`` expect both the
    NormalizedMessage fields and the flat ``text``/``username``/``ts``
    shorthand — so we emit both.
    """
    ts = msg.timestamp.isoformat()
    return {
        "content": msg.content,
        "author": msg.author,
        "author_name": msg.author_name,
        "author_image": "",
        "platform": "discord",
        "channel_id": channel_id,
        "channel_name": channel_id,
        "message_id": msg.message_id,
        "timestamp": ts,
        "thread_id": msg.thread_id,
        "attachments": msg.attachments,
        "reactions": msg.reactions,
        "reply_count": 0,
        "raw_metadata": msg.raw_metadata,
        "text": msg.content,
        "username": msg.author_name,
        "ts": msg.timestamp.timestamp(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import DiscordChatExporter CSV into the dry-run cache"
    )
    parser.add_argument("csv_path", help="Path to the exported CSV file")
    parser.add_argument(
        "--channel-id",
        help="Override channel ID (default: extracted from filename)",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Max messages to import (0 = all)"
    )
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        print(f"Error: file not found: {csv_path}")
        return

    channel_id = args.channel_id
    if not channel_id:
        m = re.search(r"\[(\d+)\]", csv_path.stem)
        channel_id = m.group(1) if m else csv_path.stem

    headers, _samples, _fmt = read_headers_and_samples(csv_path)
    preset = detect_preset(headers)
    if preset is None:
        print(
            f"Warning: headers {headers} do not match the DiscordChatExporter "
            "preset. Falling back to fuzzy matching via the generic importer."
        )
        from beever_atlas.agents.ingestion.csv_mapper import (
            infer_mapping_deterministic,
        )
        mapping_result = infer_mapping_deterministic(csv_path)
        mapping = mapping_result.mapping
        if mapping_result.needs_review:
            print(
                "Warning: fuzzy mapping is low-confidence. Review before ingesting:\n"
                + json.dumps(asdict(mapping), ensure_ascii=False, indent=2)
            )
    else:
        mapping = preset.mapping

    print(f"Channel ID: {channel_id}")
    print(f"Parsing {csv_path.name} ...")

    opts = ParseOptions(
        default_platform="discord",
        default_channel_id=channel_id,
        default_channel_name=channel_id,
        max_rows=args.limit,
    )
    messages = parse_file(csv_path, mapping, opts)
    print(f"Parsed {len(messages)} messages")

    cache_dir = Path(".omc/cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_file = cache_dir / f"messages-{channel_id}.json"
    out_file.write_text(
        json.dumps(
            [_build_cache_entry(m, channel_id) for m in messages],
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Saved to {out_file}")
    print()
    print("Now run:")
    print(f"  uv run python -m beever_atlas.scripts.dry_run {channel_id} --cached --limit 50")
    print(f"  uv run python -m beever_atlas.scripts.dry_run {channel_id} --cached --facts-only")
    print(f"  uv run python -m beever_atlas.scripts.dry_run {channel_id} --cached --batch-api")


if __name__ == "__main__":
    main()
