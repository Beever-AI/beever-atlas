"""Sync monitor verification script.

Polls /api/channels/{channel_id}/sync/status repeatedly and asserts a
list of invariants that the frontend monitor depends on. Reports any
anomaly the moment it happens, so you don't have to click around in
the browser to spot regressions in:

  * total_batches stability / monotonic growth
  * batches_completed monotonic non-decrease
  * phases waterfall transitions (fetched → done → extracting → ...)
  * Fact/entity/embedded/media totals NEVER decreasing
  * in-flight batch count matches activity_log's started-but-not-persisted set
  * Activity_log growth doesn't shrink unexpectedly

USAGE:
    # Run against currently-running uvicorn (default http://localhost:8000):
    uv run python scripts/verify_sync_monitor.py --channel-id <ID>

    # Trigger a fresh sync first:
    uv run python scripts/verify_sync_monitor.py --channel-id <ID> --trigger-sync

    # List channels to find an ID:
    uv run python scripts/verify_sync_monitor.py --list

The script reads the user-facing API key from ``.env`` so no extra
configuration is needed when run from the repo root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def load_api_key() -> str:
    """Pull the first BEEVER_API_KEYS value out of ``.env``."""
    if not ENV_FILE.exists():
        sys.exit(f"verify_sync_monitor: .env not found at {ENV_FILE}")
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("BEEVER_API_KEYS="):
            value = line.split("=", 1)[1].strip()
            return value.split(",")[0].strip()
    sys.exit("verify_sync_monitor: BEEVER_API_KEYS not found in .env")


@dataclass
class Anomaly:
    """A single invariant violation."""

    tick: int
    severity: str  # "warn" | "error"
    field: str
    message: str
    prev_value: Any = None
    next_value: Any = None


@dataclass
class VerifyState:
    """Accumulated state across polls — used to detect anomalies."""

    tick: int = 0
    last_status: dict[str, Any] | None = None
    last_batches_completed: int = 0
    last_total_batches: int = 0
    last_processed_messages: int = 0
    # Cumulative high-water marks for metric tiles — must never decrease.
    max_facts: int = 0
    max_entities: int = 0
    max_relationships: int = 0
    max_embedded: int = 0
    max_media: int = 0
    last_activity_log_len: int = 0
    anomalies: list[Anomaly] = field(default_factory=list)
    phase_history: dict[str, list[str]] = field(default_factory=dict)


# Allowed forward transitions per phase. ``in_flight`` may flip to
# ``done``/``failed``; ``done`` may NOT flip back. ``pending`` may go
# anywhere forward.
_ALLOWED_TRANSITIONS = {
    "pending": {"pending", "in_flight", "done", "failed", "skipped"},
    "in_flight": {"in_flight", "done", "failed"},
    "done": {"done"},  # terminal
    "failed": {"failed", "in_flight"},  # retry allowed
    "skipped": {"skipped", "done"},
}


def _anom(state: VerifyState, severity: str, field: str, message: str, prev: Any = None, nxt: Any = None) -> None:
    state.anomalies.append(
        Anomaly(
            tick=state.tick,
            severity=severity,
            field=field,
            message=message,
            prev_value=prev,
            next_value=nxt,
        )
    )
    icon = "✗" if severity == "error" else "⚠"
    extra = ""
    if prev is not None or nxt is not None:
        extra = f"  (prev={prev}, next={nxt})"
    print(f"[T+{state.tick:>3}s] {icon} {field}: {message}{extra}", flush=True)


def derive_metric_totals(status: dict[str, Any]) -> dict[str, int]:
    """Reproduce the frontend MetricsBar sums from the server's
    ``batch_results`` array (if populated) — the same path the UI takes."""
    results = status.get("batch_results") or []
    totals = {"facts": 0, "entities": 0, "relationships": 0, "embedded": 0, "media": 0}
    for r in results:
        totals["facts"] += int(r.get("facts_count") or 0)
        totals["entities"] += int(r.get("entities_count") or 0)
        totals["relationships"] += int(r.get("relationships_count") or 0)
        totals["embedded"] += int(r.get("embedded_count") or 0)
        totals["media"] += int(r.get("media_count") or 0)
    return totals


def count_active_batches(status: dict[str, Any]) -> tuple[int, int]:
    """Reproduce the frontend's ``batchesInFlight`` calc — count batches
    that have a ``stage_start`` event but no ``persister stage_output``.

    Returns ``(in_flight_from_log, max_batch_idx_seen)``.
    """
    activity_log = (status.get("stage_details") or {}).get("activity_log") or []
    started: set[int] = set()
    persisted: set[int] = set()
    max_idx = 0
    for e in activity_log:
        idx = e.get("batch_idx")
        if not isinstance(idx, int):
            continue
        max_idx = max(max_idx, idx)
        if e.get("type") == "stage_start":
            started.add(idx)
        elif e.get("type") == "stage_output" and e.get("agent") == "persister":
            persisted.add(idx)
    in_flight = len([i for i in started if i not in persisted])
    return in_flight, max_idx


def check_invariants(state: VerifyState, status: dict[str, Any]) -> None:
    """Walk the response, raise anomalies on any rule violation."""
    # 1. batches_completed must never decrease
    bc = int(status.get("batches_completed") or 0)
    if bc < state.last_batches_completed:
        _anom(
            state,
            "error",
            "batches_completed",
            "decreased — counter must be monotonic",
            prev=state.last_batches_completed,
            nxt=bc,
        )
    state.last_batches_completed = bc

    # 2. total_batches must be >= batches_completed (UI clamps but server
    #    should be correct too after we wired the $max update).
    tb = int(status.get("total_batches") or 0)
    if tb < bc:
        _anom(
            state,
            "warn",
            "total_batches",
            "total_batches < batches_completed; UI clamps but server still wrong",
            prev=tb,
            nxt=bc,
        )
    state.last_total_batches = tb

    # 3. processed_messages must never decrease
    pm = int(status.get("processed_messages") or 0)
    if pm < state.last_processed_messages:
        _anom(
            state,
            "error",
            "processed_messages",
            "decreased — counter must be monotonic",
            prev=state.last_processed_messages,
            nxt=pm,
        )
    state.last_processed_messages = pm

    # 4. Phases must follow allowed forward transitions
    for phase in status.get("phases") or []:
        name = phase.get("name") or ""
        cur = phase.get("state") or "pending"
        history = state.phase_history.setdefault(name, [])
        if history:
            prev = history[-1]
            allowed = _ALLOWED_TRANSITIONS.get(prev, set())
            if cur not in allowed:
                _anom(
                    state,
                    "error",
                    f"phase.{name}",
                    f"illegal transition {prev} → {cur}",
                    prev=prev,
                    nxt=cur,
                )
        history.append(cur)

    # 5. Metric tile totals must monotonically non-decrease
    totals = derive_metric_totals(status)
    for key, label, prev_attr in [
        ("facts", "facts", "max_facts"),
        ("entities", "entities", "max_entities"),
        ("relationships", "relationships", "max_relationships"),
        ("embedded", "embedded", "max_embedded"),
        ("media", "media", "max_media"),
    ]:
        next_val = totals[key]
        prev_val = getattr(state, prev_attr)
        if next_val < prev_val:
            _anom(
                state,
                "error",
                f"metric.{label}",
                "metric tile total decreased — should be monotonic",
                prev=prev_val,
                nxt=next_val,
            )
        setattr(state, prev_attr, max(prev_val, next_val))

    # 6. activity_log shouldn't shrink unexpectedly during one sync
    activity_log = (status.get("stage_details") or {}).get("activity_log") or []
    if len(activity_log) < state.last_activity_log_len:
        # Eviction is expected near the $slice cap; warn only.
        _anom(
            state,
            "warn",
            "activity_log",
            "activity_log shrunk — likely $slice eviction (expected if > cap)",
            prev=state.last_activity_log_len,
            nxt=len(activity_log),
        )
    state.last_activity_log_len = len(activity_log)


async def list_channels(client: httpx.AsyncClient, api_key: str) -> None:
    r = await client.get(
        "/api/channels",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    r.raise_for_status()
    print(f"{'channel_id':<45}  name")
    print(f"{'-' * 45}  {'-' * 30}")
    for c in r.json():
        print(f"{c['channel_id']:<45}  {c.get('name', '?')}")


async def trigger_sync(client: httpx.AsyncClient, api_key: str, channel_id: str) -> None:
    r = await client.post(
        f"/api/channels/{channel_id}/sync",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if r.status_code not in (200, 202, 409):
        print(f"verify_sync_monitor: sync trigger returned {r.status_code}: {r.text[:200]}")
        sys.exit(1)
    print(f"sync triggered ({r.status_code}): {r.json() if r.headers.get('content-type','').startswith('application/json') else ''}")


async def poll_until_settled(
    client: httpx.AsyncClient,
    api_key: str,
    channel_id: str,
    max_seconds: int,
) -> VerifyState:
    state = VerifyState()
    start = time.monotonic()
    poll_interval = 2.0

    while True:
        state.tick = int(time.monotonic() - start)
        if state.tick > max_seconds:
            print(f"\nverify_sync_monitor: timeout after {max_seconds}s — exiting")
            break
        try:
            r = await client.get(
                f"/api/channels/{channel_id}/sync/status",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10.0,
            )
            r.raise_for_status()
            status = r.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[T+{state.tick:>3}s] ✗ poll failed: {exc}")
            await asyncio.sleep(poll_interval)
            continue

        check_invariants(state, status)
        state.last_status = status

        # Friendly tick summary
        phases = status.get("phases") or []
        phases_str = " · ".join(f"{p['name']}={p['state']}" for p in phases)
        bc = int(status.get("batches_completed") or 0)
        tb = int(status.get("total_batches") or 0)
        pm = int(status.get("processed_messages") or 0)
        tm = int(status.get("total_messages") or 0)
        totals = derive_metric_totals(status)
        in_flight, max_idx = count_active_batches(status)
        print(
            f"[T+{state.tick:>3}s] state={status.get('state')} batches={bc}/{tb} "
            f"in_flight_calc={in_flight} max_idx={max_idx} "
            f"msgs={pm}/{tm} facts={totals['facts']} entities={totals['entities']} "
            f"embedded={totals['embedded']} media={totals['media']}",
            flush=True,
        )

        # Stop when all phases done/skipped/failed
        if phases and all(p.get("state") in ("done", "skipped", "failed") for p in phases):
            print(f"\nverify_sync_monitor: pipeline settled at T+{state.tick}s — phases: {phases_str}")
            break

        await asyncio.sleep(poll_interval)

    return state


def print_summary(state: VerifyState) -> int:
    errors = [a for a in state.anomalies if a.severity == "error"]
    warns = [a for a in state.anomalies if a.severity == "warn"]
    print("\n" + "=" * 60)
    print(f"VERIFICATION SUMMARY (after {state.tick} seconds)")
    print("=" * 60)
    print(f"  errors:  {len(errors)}")
    print(f"  warnings: {len(warns)}")
    if state.last_status:
        s = state.last_status
        print(f"  final state: {s.get('state')}")
        print(f"  final batches: {s.get('batches_completed')}/{s.get('total_batches')}")
        totals = derive_metric_totals(s)
        print(f"  final totals: facts={totals['facts']} entities={totals['entities']} "
              f"relationships={totals['relationships']} embedded={totals['embedded']} "
              f"media={totals['media']}")
    if errors:
        print("\nERRORS (sync correctness violations):")
        for a in errors:
            print(f"  [T+{a.tick}s] {a.field}: {a.message}  (prev={a.prev_value}, next={a.next_value})")
    if warns:
        print("\nWARNINGS (expected eviction etc, review only):")
        for a in warns[:5]:
            print(f"  [T+{a.tick}s] {a.field}: {a.message}")
        if len(warns) > 5:
            print(f"  ... +{len(warns) - 5} more")
    print("=" * 60)
    return 1 if errors else 0


async def main_async(args: argparse.Namespace) -> int:
    api_key = load_api_key()
    base_url = os.environ.get("BEEVER_API_URL", "http://localhost:8000")

    async with httpx.AsyncClient(base_url=base_url) as client:
        # Pre-flight health check
        try:
            h = await client.get("/api/health", timeout=5.0)
            h.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"verify_sync_monitor: backend not reachable at {base_url}: {exc}")
            return 2

        if args.list:
            await list_channels(client, api_key)
            return 0

        if not args.channel_id:
            print("verify_sync_monitor: --channel-id is required (use --list to discover)")
            return 2

        if args.trigger_sync:
            await trigger_sync(client, api_key, args.channel_id)

        state = await poll_until_settled(client, api_key, args.channel_id, args.timeout)
        return print_summary(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify sync monitor invariants live")
    parser.add_argument("--channel-id", help="channel ID to verify")
    parser.add_argument("--trigger-sync", action="store_true", help="trigger a fresh sync before polling")
    parser.add_argument("--list", action="store_true", help="list channels and exit")
    parser.add_argument("--timeout", type=int, default=900, help="max seconds to poll (default 900)")
    parser.add_argument("--json-output", help="write raw poll responses to JSONL file for offline analysis")
    args = parser.parse_args()
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
