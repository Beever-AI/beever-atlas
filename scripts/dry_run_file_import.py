"""Dry-run the file-import pipeline end-to-end against a local file.

Usage:
    uv run python scripts/dry_run_file_import.py <path_to_file> [--llm] [--limit N]
                                                 [--encoding ENC] [--dayfirst]

Prints the detected encoding / format, the inferred column mapping, and the
first few parsed NormalizedMessage objects. Does NOT write to Weaviate /
Neo4j / Mongo — use ``scripts/ingest_from_csv.py`` for that once you're
happy with the mapping.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from beever_atlas.agents.ingestion.csv_mapper import (
    infer_mapping,
    infer_mapping_deterministic,
)
from beever_atlas.services.file_importer import (
    ParseOptions,
    detect_encoding,
    detect_format,
    parse_file,
)


def _pretty(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Path to CSV / TSV / JSONL file")
    parser.add_argument("--llm", action="store_true", help="Enable LLM mapping inference (default: off)")
    parser.add_argument("--limit", type=int, default=5, help="Print this many parsed messages (default: 5)")
    parser.add_argument("--encoding", default=None, help="Force an encoding instead of auto-detecting")
    parser.add_argument("--dayfirst", action="store_true", help="Parse ambiguous dates as DD/MM")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"File not found: {path}")

    print("=" * 70)
    print(f"File: {path}")
    print("=" * 70)

    enc = args.encoding or detect_encoding(path)
    fmt = detect_format(path)
    print(f"Encoding:  {enc}")
    print(f"Format:    {fmt}")
    print()

    if args.llm:
        result = await infer_mapping(path, use_llm=True)
    else:
        result = infer_mapping_deterministic(path)

    print("-- Mapping inference --")
    print(f"Source:             {result.source}")
    print(f"Preset:             {result.preset}")
    print(f"Overall confidence: {result.overall_confidence}")
    print(f"Needs review:       {result.needs_review}")
    if result.detected_source:
        print(f"Detected source:    {result.detected_source}")
    if result.notes:
        print(f"Notes:              {result.notes}")
    print("Per-field confidence:")
    print(_pretty(result.confidence))
    print("Column mapping:")
    print(_pretty(asdict(result.mapping)))
    print()

    print("-- Parsed sample --")
    opts = ParseOptions(dayfirst=args.dayfirst, max_rows=args.limit or 5)
    messages = parse_file(path, result.mapping, opts, encoding=enc)
    print(f"Parsed {len(messages)} message(s) (limited to {opts.max_rows}):")
    for i, m in enumerate(messages, 1):
        print(f"\n[{i}]")
        print(f"  id:         {m.message_id}")
        print(f"  ts:         {m.timestamp.isoformat()}")
        print(f"  author:     {m.author} ({m.author_name})")
        print(f"  content:    {m.content[:120]!r}")
        if m.attachments:
            print(f"  attachments: {m.attachments}")
        if m.reactions:
            print(f"  reactions:   {m.reactions}")

    print()
    print("Done. To ingest this file for real, next steps are:")
    print("  1. Verify the mapping above looks right.")
    print("  2. Run: uv run python -m beever_atlas.scripts.import_discord_csv <path>  (preset path)")
    print("     or use the /imports/commit API endpoint once it lands (Phase 3).")


if __name__ == "__main__":
    asyncio.run(main())
