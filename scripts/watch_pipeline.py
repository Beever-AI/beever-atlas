"""Continuous pipeline watcher.

Runs scripts/test_pipeline_design.py against whichever channel is
currently syncing, reports verdicts to stdout AND appends to a
rolling JSONL log. Designed to run in the background while you
develop — every sync that fires gets validated automatically.

Workflow:

  1. Poll ``/api/channels`` every ``--poll-seconds`` (default 30s).
  2. When a channel's ``sync_state`` flips to ``syncing`` OR any phase
     reports ``in_flight``, kick off a verification run against it.
  3. Run completes when the sync settles or hits the per-run timeout.
  4. Append the resulting JSON report to ``--log-file`` (one line per run).
  5. Print a one-line summary (PASS / FAIL + key numbers).

Use Ctrl-C to exit. The watcher never modifies channel state — it
ONLY observes.

USAGE:
    # Watch indefinitely:
    uv run python scripts/watch_pipeline.py

    # Watch a specific channel only (skips auto-discovery):
    uv run python scripts/watch_pipeline.py --channel-id <ID>

    # Trigger fresh syncs on a target channel every N minutes for soak:
    uv run python scripts/watch_pipeline.py --channel-id <ID> --auto-trigger --trigger-interval-min 15

    # Log to a custom location:
    uv run python scripts/watch_pipeline.py --log-file /tmp/pipeline-runs.jsonl

Reading the log:
    tail -f /tmp/pipeline-runs.jsonl | jq '{verdict, errors, perf: .section_d_performance}'
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
TEST_SCRIPT = ROOT / "scripts" / "test_pipeline_design.py"

DEFAULT_POLL_SECONDS = 30
DEFAULT_LOG_FILE = "/tmp/pipeline-watch.jsonl"
DEFAULT_PER_RUN_TIMEOUT = 1500  # 25 min per single sync


def load_api_key() -> str:
    if not ENV_FILE.exists():
        sys.exit(f"watch_pipeline: .env not found at {ENV_FILE}")
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("BEEVER_API_KEYS="):
            return line.split("=", 1)[1].strip().split(",")[0].strip()
    sys.exit("watch_pipeline: BEEVER_API_KEYS not found in .env")


async def find_active_channel(client: httpx.AsyncClient, api_key: str) -> str | None:
    """Pick the first channel that appears to be in an active sync.

    Checks each channel's ``/sync/status`` and returns the channel_id
    of the first one whose state is ``syncing`` OR has any phase
    in ``in_flight``. Returns None when nothing is running.
    """
    try:
        r = await client.get(
            "/api/channels",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"  watch: channel list failed: {exc}", flush=True)
        return None

    channels = r.json() if isinstance(r.json(), list) else []
    for c in channels:
        cid = c.get("channel_id") or c.get("id")
        if not cid:
            continue
        try:
            sr = await client.get(
                f"/api/channels/{cid}/sync/status",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5.0,
            )
            sr.raise_for_status()
            status = sr.json()
        except Exception:
            continue
        state = status.get("state")
        phases = status.get("phases") or []
        in_flight_any = any(p.get("state") == "in_flight" for p in phases)
        if state == "syncing" or in_flight_any:
            return cid
    return None


async def trigger_sync(client: httpx.AsyncClient, api_key: str, channel_id: str) -> None:
    """Best-effort sync trigger — used in --auto-trigger soak mode."""
    try:
        await client.post(
            f"/api/channels/{channel_id}/sync",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        print(f"  watch: triggered sync on {channel_id}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  watch: sync trigger failed for {channel_id}: {exc}", flush=True)


async def run_verification(channel_id: str, timeout: int) -> dict[str, Any] | None:
    """Invoke test_pipeline_design.py as a subprocess and parse JSON output."""
    out_path = Path(f"/tmp/watch-pipeline-{channel_id}-{int(asyncio.get_event_loop().time())}.json")
    cmd = [
        "uv", "run", "python", str(TEST_SCRIPT),
        "--channel-id", channel_id,
        "--timeout", str(timeout),
        "--json-out", str(out_path),
        "--quiet",
    ]
    print(f"\n  watch: starting verification — channel={channel_id} timeout={timeout}s", flush=True)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    # Stream child output so we see the verifier's live tick lines.
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        print(f"    {line.decode().rstrip()}", flush=True)
    await proc.wait()
    finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        report = json.loads(out_path.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"  watch: failed to read report at {out_path}: {exc}", flush=True)
        return None
    report["watch_started_at"] = started_at
    report["watch_finished_at"] = finished_at
    report["exit_code"] = proc.returncode
    return report


def append_log(log_file: str, report: dict[str, Any]) -> None:
    with open(log_file, "a") as f:
        f.write(json.dumps(report) + "\n")


def summary_line(report: dict[str, Any]) -> str:
    a = report.get("section_a_realtime") or {}
    b = report.get("section_b_ordering") or {}
    return (
        f"verdict={report.get('verdict')} "
        f"errors={report.get('errors')} "
        f"batches={a.get('final_batches_completed')}/{a.get('final_total_batches')} "
        f"per-batch p50={a.get('per_batch_seconds_median', '-')}s "
        f"wiki_tail={b.get('wiki_tail_seconds', '-')}s "
        f"duration={report.get('duration_seconds')}s"
    )


async def main_async(args: argparse.Namespace) -> int:
    api_key = load_api_key()
    base_url = os.environ.get("BEEVER_API_URL", "http://localhost:8000")
    log_file = args.log_file

    last_trigger_t = 0.0

    print(
        f"watch_pipeline: started — log={log_file} poll={args.poll_seconds}s "
        f"per-run-timeout={args.per_run_timeout}s",
        flush=True,
    )
    async with httpx.AsyncClient(base_url=base_url) as client:
        while True:
            try:
                # Health pre-check
                await client.get("/api/health", timeout=5.0)
            except Exception:
                print("  watch: backend unreachable; sleeping…", flush=True)
                await asyncio.sleep(args.poll_seconds)
                continue

            target_channel = args.channel_id
            if not target_channel:
                target_channel = await find_active_channel(client, api_key)

            # Optional soak mode: trigger fresh syncs on a fixed cadence.
            if args.channel_id and args.auto_trigger:
                now = asyncio.get_event_loop().time()
                if now - last_trigger_t > args.trigger_interval_min * 60:
                    await trigger_sync(client, api_key, args.channel_id)
                    last_trigger_t = now
                    # Give the backend a moment to spin up the sync row.
                    await asyncio.sleep(3)

            if not target_channel:
                print(f"  watch: no active sync — sleeping {args.poll_seconds}s", flush=True)
                await asyncio.sleep(args.poll_seconds)
                continue

            # Run a verification cycle synchronously.
            report = await run_verification(target_channel, args.per_run_timeout)
            if report is None:
                await asyncio.sleep(args.poll_seconds)
                continue

            print(f"\n  watch: ▶  {summary_line(report)}", flush=True)
            append_log(log_file, report)

            if args.once:
                return 0 if report.get("verdict") == "PASS" else 1

            # Loop back to wait for the next sync trigger.
            await asyncio.sleep(args.poll_seconds)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--channel-id", help="watch only this channel (skip auto-discovery)")
    p.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS,
                   help=f"sleep between polls when no active sync (default {DEFAULT_POLL_SECONDS})")
    p.add_argument("--per-run-timeout", type=int, default=DEFAULT_PER_RUN_TIMEOUT,
                   help=f"max seconds per single verification run (default {DEFAULT_PER_RUN_TIMEOUT})")
    p.add_argument("--log-file", default=DEFAULT_LOG_FILE,
                   help=f"JSONL output path (default {DEFAULT_LOG_FILE})")
    p.add_argument("--auto-trigger", action="store_true",
                   help="(with --channel-id) trigger fresh syncs every --trigger-interval-min")
    p.add_argument("--trigger-interval-min", type=int, default=15,
                   help="minutes between auto-triggers in soak mode (default 15)")
    p.add_argument("--once", action="store_true",
                   help="run a single verification then exit (exit 0=PASS, 1=FAIL)")
    args = p.parse_args()
    try:
        sys.exit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        print("\nwatch_pipeline: stopped by user", flush=True)


if __name__ == "__main__":
    main()
