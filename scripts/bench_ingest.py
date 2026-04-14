"""Benchmark + stability harness for the ingestion pipeline.

Usage:
    uv run python scripts/bench_ingest.py <channel_id> <connection_id> [--full|--incremental]

Environment:
    BEEVER_API_BASE    default http://localhost:8000
    BEEVER_API_KEY     sent as "Authorization: Bearer <key>" (required)
                        — use any value from BEEVER_API_KEYS in backend .env,
                          or the BRIDGE_API_KEY.
    BEEVER_ADMIN_TOKEN optional, sent as X-Admin-Token for dev endpoints.

What it does:
    1. POST /api/channels/{id}/sync to trigger a sync.
    2. Poll /api/channels/{id}/sync/status every 2s.
    3. Print live table: elapsed, state, stage, batch progress, processed.
    4. On completion, print a report:
         - Total wall-clock
         - Per-stage aggregate time
         - Per-batch timings + fact/entity counts
         - Truncation events count (parsed from server logs if present in stage_details)
         - Job-level and per-batch errors
         - Pass/fail verdict

Exits 0 on success, 1 on failure/interruption, 2 on script error.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any

import httpx


def _headers() -> dict[str, str]:
    h: dict[str, str] = {}
    key = os.environ.get("BEEVER_API_KEY")
    if not key:
        print(
            "ERROR: BEEVER_API_KEY is required.\n"
            "  Set it to any value from BEEVER_API_KEYS in your backend .env,\n"
            "  or to the BRIDGE_API_KEY. Example:\n"
            "    BEEVER_API_KEY=$(grep ^BRIDGE_API_KEY .env | cut -d= -f2-) \\\n"
            "      uv run python scripts/bench_ingest.py <channel_id> <connection_id>",
            file=sys.stderr,
        )
        sys.exit(2)
    h["Authorization"] = f"Bearer {key}"
    if v := os.environ.get("BEEVER_ADMIN_TOKEN"):
        h["X-Admin-Token"] = v
    return h


def _fmt_secs(s: float) -> str:
    if s < 1:
        return f"{s*1000:.0f}ms"
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(s, 60)
    return f"{int(m)}m{sec:02.0f}s"


def _clear_line() -> None:
    sys.stdout.write("\r\033[K")


async def _trigger(
    client: httpx.AsyncClient, base: str, channel_id: str, connection_id: str, sync_type: str
) -> str:
    r = await client.post(
        f"{base}/api/channels/{channel_id}/sync",
        params={"sync_type": sync_type, "connection_id": connection_id},
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("job_id") or data.get("id") or "unknown"


async def _poll(
    client: httpx.AsyncClient, base: str, channel_id: str
) -> dict[str, Any]:
    r = await client.get(
        f"{base}/api/channels/{channel_id}/sync/status", timeout=15.0
    )
    r.raise_for_status()
    return r.json()


def _render_live(status: dict[str, Any], elapsed: float) -> None:
    state = status.get("state", "?")
    stage = status.get("current_stage") or "-"
    done = status.get("batches_completed", 0)
    total = status.get("total_batches", 0)
    processed = status.get("processed_messages", 0)
    messages = status.get("parent_messages") or status.get("total_messages") or 0
    pct = (processed / messages * 100) if messages else 0
    line = (
        f"[{_fmt_secs(elapsed)}] state={state:<8} "
        f"batches={done}/{total}  msgs={processed}/{messages} ({pct:5.1f}%)  "
        f"stage={stage[:40]}"
    )
    _clear_line()
    sys.stdout.write(line)
    sys.stdout.flush()


def _report(status: dict[str, Any], wall_clock: float) -> int:
    print("\n")
    print("=" * 72)
    print(f"  Ingestion Benchmark Report")
    print("=" * 72)
    state = status.get("state", "?")
    messages = status.get("total_messages", 0)
    processed = status.get("processed_messages", 0)
    total_batches = status.get("total_batches", 0)
    completed = status.get("batches_completed", 0)
    throughput = processed / wall_clock if wall_clock > 0 else 0

    verdict = "PASS" if state == "idle" else "FAIL"
    print(f"  Verdict:        {verdict}  (final state: {state})")
    print(f"  Wall clock:     {_fmt_secs(wall_clock)}")
    print(f"  Messages:       {processed}/{messages} processed")
    print(f"  Batches:        {completed}/{total_batches} completed")
    print(f"  Throughput:     {throughput:.2f} msg/s  ({throughput*60:.1f} msg/min)")

    timings = status.get("stage_timings") or {}
    if timings:
        print("\n  Per-stage aggregate time:")
        for stage in ("preprocessor", "fact_extractor", "entity_extractor",
                      "embedder", "cross_batch_validator", "persister"):
            t = timings.get(stage)
            if t is not None:
                print(f"    {stage:<22} {_fmt_secs(float(t))}")

    batch_results = status.get("batch_results") or []
    if batch_results:
        print(f"\n  Per-batch ({len(batch_results)}):")
        print(f"    {'#':<3} {'facts':>6} {'ents':>6} {'rels':>6} {'time':>8}  errors")
        for br in batch_results:
            num = br.get("batch_num", "?")
            facts = br.get("facts_count", br.get("facts", 0))
            ents = br.get("entities_count", br.get("entities", 0))
            rels = br.get("relationships_count", br.get("relationships", 0))
            dur = br.get("duration_seconds") or 0
            err = br.get("error")
            errs = [err] if err else (br.get("errors") or [])
            err_str = "-" if not errs else f"{len(errs)}: {str(errs[0])[:40]}"
            print(f"    {num:<3} {facts:>6} {ents:>6} {rels:>6} {_fmt_secs(float(dur)):>8}  {err_str}")

    errors = status.get("errors") or []
    if errors:
        print(f"\n  Job-level errors ({len(errors)}):")
        for e in errors[:10]:
            print(f"    - {e}")
        if len(errors) > 10:
            print(f"    ... +{len(errors)-10} more")

    # Stability signals from stage_details (truncation, agent failures, etc.)
    details = status.get("stage_details") or {}
    activity = details.get("activity_log") or []
    truncations = [e for e in activity if "truncat" in str(e).lower()]
    if truncations:
        print(f"\n  ⚠ Truncation events: {len(truncations)}")

    print("=" * 72)
    return 0 if verdict == "PASS" else 1


async def _run(channel_id: str, connection_id: str, sync_type: str, timeout_s: int) -> int:
    base = os.environ.get("BEEVER_API_BASE", "http://localhost:8000")
    async with httpx.AsyncClient(headers=_headers()) as client:
        try:
            job_id = await _trigger(client, base, channel_id, connection_id, sync_type)
        except httpx.HTTPStatusError as e:
            print(f"Trigger failed: {e.response.status_code} {e.response.text}", file=sys.stderr)
            return 2
        except httpx.RequestError as e:
            print(f"Trigger failed: {e}", file=sys.stderr)
            return 2

        print(f"Started sync job_id={job_id} on {channel_id} ({sync_type})")
        print(f"API: {base}  (set BEEVER_API_BASE to override)")
        print("Polling every 2s… Ctrl-C to abort (sync will keep running server-side).\n")

        t0 = time.monotonic()
        last_status: dict[str, Any] = {}
        while True:
            elapsed = time.monotonic() - t0
            if elapsed > timeout_s:
                print(f"\nTIMEOUT: exceeded {timeout_s}s", file=sys.stderr)
                return 1
            try:
                status = await _poll(client, base, channel_id)
            except Exception as exc:  # noqa: BLE001
                _clear_line()
                print(f"poll error: {exc}", file=sys.stderr)
                await asyncio.sleep(2)
                continue
            last_status = status
            _render_live(status, elapsed)
            state = status.get("state")
            if state in ("idle", "error"):
                break
            await asyncio.sleep(2)

        return _report(last_status, time.monotonic() - t0)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("channel_id")
    p.add_argument("connection_id")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--full", action="store_const", const="full", dest="sync_type")
    group.add_argument("--incremental", action="store_const", const="incremental", dest="sync_type")
    p.add_argument("--timeout", type=int, default=1800, help="max seconds to wait (default 1800)")
    args = p.parse_args()
    sync_type = args.sync_type or "auto"

    try:
        rc = asyncio.run(_run(args.channel_id, args.connection_id, sync_type, args.timeout))
    except KeyboardInterrupt:
        print("\nAborted by user. Server-side sync may still be running.", file=sys.stderr)
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
