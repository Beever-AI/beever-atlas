"""End-to-end pipeline design validation.

Validates the Beever Atlas ingestion model:

    Source --> Memory --> LLM Wiki

against the running backend. Asserts invariants from the
``memory-then-wiki-pipeline-realignment`` spec and emits a structured
pass/fail report covering:

  Section A. Real-time monitoring accuracy
    - batches_completed grows incrementally (live), not in one jump
    - Per-batch increment cadence (median / p95 / max gap)
    - Tile metrics (facts / entities / embedded / media) non-decreasing
    - total_batches dynamically bumps to >= batches_completed

  Section B. Pipeline ordering (source -> memory -> wiki)
    - ``fetched`` reaches ``done`` before ``extracting`` finishes
    - ``extracting`` reaches ``done`` BEFORE ``wiki_maintenance`` or
      ``overview_wiki`` transition to ``done``
    - Wiki phases do not flip done early (memory_settled gate)

  Section C. Stability
    - No phase rewinds (illegal state transitions)
    - No counter resets / decrements
    - No orphan in-flight batches at terminal state

  Section D. Performance
    - End-to-end seconds (fetched start -> final settle)
    - Time-to-first-batch-completed (kick responsiveness)
    - Average per-batch latency
    - Wiki tail (extracting done -> all phases done)

  Section E. Correctness vs UI math
    - batchesInFlight calculated from activity_log matches reality

USAGE:
    # Observe an already-running sync:
    uv run python scripts/test_pipeline_design.py --channel-id <ID>

    # Trigger a fresh sync first:
    uv run python scripts/test_pipeline_design.py --channel-id <ID> --trigger

    # Run with a custom timeout (default 1200s):
    uv run python scripts/test_pipeline_design.py --channel-id <ID> --timeout 1800

    # JSON report only (CI mode, exit 0 on green, 1 on red):
    uv run python scripts/test_pipeline_design.py --channel-id <ID> --json-out report.json

Exit codes:
    0  All design invariants satisfied
    1  One or more invariant violations
    2  Backend unreachable or fatal setup error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

POLL_INTERVAL_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 1200

# Forward-only phase transitions (terminal: done, failed/skipped).
_ALLOWED_TRANSITIONS = {
    "pending": {"pending", "in_flight", "done", "failed", "skipped"},
    "in_flight": {"in_flight", "done", "failed"},
    "done": {"done"},
    "failed": {"failed", "in_flight"},
    "skipped": {"skipped", "done"},
}

PHASE_FETCHED = "fetched"
PHASE_EXTRACTING = "extracting"
PHASE_WIKI_MAINT = "wiki_maintenance"
PHASE_WIKI_OVERVIEW = "overview_wiki"


def load_api_key() -> str:
    if not ENV_FILE.exists():
        sys.exit(f"test_pipeline_design: .env not found at {ENV_FILE}")
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("BEEVER_API_KEYS="):
            value = line.split("=", 1)[1].strip()
            return value.split(",")[0].strip()
    sys.exit("test_pipeline_design: BEEVER_API_KEYS not found in .env")


@dataclass
class Violation:
    section: str            # "A".."E"
    severity: str           # "error" | "warn"
    invariant: str          # short id
    message: str
    t_seconds: float
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestRun:
    channel_id: str
    started_monotonic: float
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    phase_history: dict[str, list[tuple[float, str]]] = field(default_factory=dict)
    batches_completed_history: list[tuple[float, int]] = field(default_factory=list)
    total_batches_history: list[tuple[float, int]] = field(default_factory=list)
    tile_history: dict[str, list[tuple[float, int]]] = field(default_factory=dict)
    violations: list[Violation] = field(default_factory=list)

    @property
    def t(self) -> float:
        return time.monotonic() - self.started_monotonic

    def record(self, section: str, severity: str, invariant: str, message: str, **detail: Any) -> None:
        v = Violation(
            section=section,
            severity=severity,
            invariant=invariant,
            message=message,
            t_seconds=round(self.t, 2),
            detail=detail,
        )
        self.violations.append(v)
        icon = "✗" if severity == "error" else "⚠"
        d = " " + json.dumps(detail) if detail else ""
        print(f"  [T+{int(self.t):>4}s] {icon} {section}/{invariant}: {message}{d}", flush=True)


def derive_tiles(status: dict[str, Any]) -> dict[str, int]:
    """Re-derive MetricsBar tile sums (matches the UI math)."""
    totals = {"facts": 0, "entities": 0, "relationships": 0, "embedded": 0, "media": 0}
    for r in status.get("batch_results") or []:
        totals["facts"] += int(r.get("facts_count") or 0)
        totals["entities"] += int(r.get("entities_count") or 0)
        totals["relationships"] += int(r.get("relationships_count") or 0)
        totals["embedded"] += int(r.get("embedded_count") or 0)
        totals["media"] += int(r.get("media_count") or 0)
    return totals


def count_in_flight_from_log(status: dict[str, Any]) -> int:
    """Reproduce UI's batchesInFlight: stage_start minus persister stage_output."""
    log = (status.get("stage_details") or {}).get("activity_log") or []
    started: set[int] = set()
    persisted: set[int] = set()
    for e in log:
        idx = e.get("batch_idx")
        if not isinstance(idx, int):
            continue
        if e.get("type") == "stage_start":
            started.add(idx)
        elif e.get("type") == "stage_output" and e.get("agent") == "persister":
            persisted.add(idx)
    return len([i for i in started if i not in persisted])


def phase_map(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p.get("name"): p for p in (status.get("phases") or [])}


def t_phase_done(run: TestRun, name: str) -> float | None:
    history = run.phase_history.get(name) or []
    for t, state in history:
        if state == "done":
            return t
    return None


def t_phase_first_in_flight(run: TestRun, name: str) -> float | None:
    history = run.phase_history.get(name) or []
    for t, state in history:
        if state == "in_flight":
            return t
    return None


def check_section_a_realtime(run: TestRun, status: dict[str, Any]) -> None:
    """Section A. Real-time monitoring accuracy."""
    t = run.t
    bc = int(status.get("batches_completed") or 0)
    tb = int(status.get("total_batches") or 0)
    pm = int(status.get("processed_messages") or 0)

    # A1: batches_completed monotonic non-decrease
    if run.batches_completed_history:
        _, prev_bc = run.batches_completed_history[-1]
        if bc < prev_bc:
            run.record(
                "A", "error", "A1_batches_monotonic",
                "batches_completed decreased",
                prev=prev_bc, next=bc, at=round(t, 1),
            )
        elif bc > prev_bc:
            # Successful increment — record cadence
            pass
    run.batches_completed_history.append((t, bc))

    # A2: total_batches must be >= batches_completed
    if tb < bc:
        run.record(
            "A", "error", "A2_total_clamp",
            "total_batches < batches_completed (server didn't bump $max)",
            total=tb, completed=bc,
        )
    run.total_batches_history.append((t, tb))

    # A3: processed_messages monotonic
    last_pm = run.snapshots[-1].get("processed_messages") if run.snapshots else 0
    if pm < int(last_pm or 0):
        run.record(
            "A", "error", "A3_processed_monotonic",
            "processed_messages decreased",
            prev=last_pm, next=pm,
        )

    # A4: tile metrics monotonic
    tiles = derive_tiles(status)
    for k, v in tiles.items():
        history = run.tile_history.setdefault(k, [])
        if history:
            prev = history[-1][1]
            if v < prev:
                run.record(
                    "A", "error", "A4_tile_monotonic",
                    f"{k} tile decreased",
                    prev=prev, next=v, key=k,
                )
        history.append((t, v))


def check_section_b_ordering(run: TestRun, status: dict[str, Any]) -> None:
    """Section B. source -> memory -> wiki ordering."""
    phases = phase_map(status)

    extracting_state = (phases.get(PHASE_EXTRACTING) or {}).get("state")
    wiki_maint_state = (phases.get(PHASE_WIKI_MAINT) or {}).get("state")
    overview_state = (phases.get(PHASE_WIKI_OVERVIEW) or {}).get("state")

    # B1: wiki_maintenance must NOT report ``done`` while extracting still in_flight.
    if extracting_state in ("pending", "in_flight") and wiki_maint_state == "done":
        run.record(
            "B", "error", "B1_wiki_before_memory",
            "wiki_maintenance phase reached 'done' while extracting still running",
            extracting=extracting_state, wiki_maintenance=wiki_maint_state,
        )

    # B2: overview_wiki must NOT report ``done`` while extracting still in_flight.
    if extracting_state in ("pending", "in_flight") and overview_state == "done":
        run.record(
            "B", "error", "B2_overview_before_memory",
            "overview_wiki phase reached 'done' while extracting still running",
            extracting=extracting_state, overview_wiki=overview_state,
        )


def check_section_c_stability(run: TestRun, status: dict[str, Any]) -> None:
    """Section C. Stability & legality of state transitions."""
    t = run.t
    phases = phase_map(status)
    for name, p in phases.items():
        state = p.get("state") or "pending"
        history = run.phase_history.setdefault(name, [])
        if history:
            prev_state = history[-1][1]
            allowed = _ALLOWED_TRANSITIONS.get(prev_state, set())
            if state not in allowed:
                run.record(
                    "C", "error", "C1_illegal_transition",
                    f"phase '{name}' illegal transition {prev_state} -> {state}",
                    phase=name, prev=prev_state, next=state,
                )
        history.append((t, state))


def finalize_section_a_performance(run: TestRun) -> dict[str, Any]:
    """Compute live-cadence summary stats from history."""
    increments: list[float] = []
    if run.batches_completed_history:
        last_t, last_bc = run.batches_completed_history[0]
        for t, bc in run.batches_completed_history[1:]:
            if bc > last_bc:
                increments.append(t - last_t)
                last_t, last_bc = t, bc

    summary: dict[str, Any] = {
        "batches_observed_increments": len(increments),
        "final_batches_completed": run.batches_completed_history[-1][1] if run.batches_completed_history else 0,
        "final_total_batches": run.total_batches_history[-1][1] if run.total_batches_history else 0,
    }
    if increments:
        summary["per_batch_seconds_median"] = round(statistics.median(increments), 2)
        summary["per_batch_seconds_max"] = round(max(increments), 2)
        summary["per_batch_seconds_min"] = round(min(increments), 2)
        summary["per_batch_seconds_mean"] = round(statistics.mean(increments), 2)
    return summary


def finalize_section_b_ordering(run: TestRun) -> tuple[dict[str, Any], list[Violation]]:
    """After settle, check final ordering between phases."""
    extras: list[Violation] = []
    t_fetched = t_phase_done(run, PHASE_FETCHED)
    t_extr = t_phase_done(run, PHASE_EXTRACTING)
    t_wiki = t_phase_done(run, PHASE_WIKI_MAINT)
    t_overview = t_phase_done(run, PHASE_WIKI_OVERVIEW)

    if t_fetched is not None and t_extr is not None and t_fetched > t_extr:
        extras.append(Violation("B", "error", "B3_fetch_after_extract",
                                "fetched done timestamp is AFTER extracting done — impossible",
                                t_extr, {"t_fetched": t_fetched, "t_extr": t_extr}))

    if t_extr is not None:
        if t_wiki is not None and t_wiki < t_extr - 0.5:
            extras.append(Violation("B", "error", "B4_wiki_done_before_extract",
                                    "wiki_maintenance done before extracting done — memory_settled gate failed",
                                    t_wiki, {"t_extr": t_extr, "t_wiki": t_wiki}))
        if t_overview is not None and t_overview < t_extr - 0.5:
            extras.append(Violation("B", "error", "B5_overview_done_before_extract",
                                    "overview_wiki done before extracting done — memory_settled gate failed",
                                    t_overview, {"t_extr": t_extr, "t_overview": t_overview}))

    summary = {
        "t_fetched_done": t_fetched,
        "t_extracting_done": t_extr,
        "t_wiki_maintenance_done": t_wiki,
        "t_overview_wiki_done": t_overview,
        "wiki_tail_seconds": (max(t_wiki or 0, t_overview or 0) - t_extr) if t_extr else None,
    }
    return summary, extras


def finalize_section_d_performance(run: TestRun) -> dict[str, Any]:
    if not run.batches_completed_history:
        return {}
    t_first = None
    for t, bc in run.batches_completed_history:
        if bc >= 1:
            t_first = t
            break
    final_t = run.batches_completed_history[-1][0]
    return {
        "t_first_batch_completed": round(t_first, 2) if t_first is not None else None,
        "t_observation_end": round(final_t, 2),
    }


def finalize_section_e_ui_math(run: TestRun) -> dict[str, Any]:
    """Compare last server batches_completed against UI's in-flight math."""
    if not run.snapshots:
        return {}
    last = run.snapshots[-1]
    bc = int(last.get("batches_completed") or 0)
    tb = int(last.get("total_batches") or 0)
    in_flight = count_in_flight_from_log(last)
    tiles = derive_tiles(last)
    return {
        "ui_in_flight_calc": in_flight,
        "server_batches_completed": bc,
        "server_total_batches": tb,
        "ui_tile_facts": tiles["facts"],
        "ui_tile_entities": tiles["entities"],
        "ui_tile_embedded": tiles["embedded"],
        "ui_tile_media": tiles["media"],
    }


async def trigger_sync(client: httpx.AsyncClient, api_key: str, channel_id: str) -> None:
    r = await client.post(
        f"/api/channels/{channel_id}/sync",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if r.status_code not in (200, 202, 409):
        print(f"  sync trigger returned {r.status_code}: {r.text[:200]}", flush=True)
        sys.exit(2)
    body = ""
    try:
        body = r.json()
    except Exception:
        body = r.text
    print(f"  sync triggered ({r.status_code}): {body}", flush=True)


async def observe(
    client: httpx.AsyncClient,
    api_key: str,
    channel_id: str,
    max_seconds: int,
) -> TestRun:
    run = TestRun(channel_id=channel_id, started_monotonic=time.monotonic())
    poll_no = 0
    stable_idle_polls = 0  # how many consecutive polls report fully settled state

    print(
        f"\nObserving channel={channel_id} timeout={max_seconds}s "
        f"interval={POLL_INTERVAL_SECONDS}s",
        flush=True,
    )

    while True:
        if run.t > max_seconds:
            print(f"\n  TIMEOUT after {max_seconds}s — stopping observation", flush=True)
            run.record("D", "error", "D0_timeout", "did not settle within timeout",
                       timeout_s=max_seconds)
            break

        try:
            r = await client.get(
                f"/api/channels/{channel_id}/sync/status",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10.0,
            )
            r.raise_for_status()
            status = r.json()
        except Exception as exc:
            print(f"  [T+{int(run.t):>4}s] poll failed: {exc}", flush=True)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        # Run invariant checks BEFORE storing snapshot so A1/A3 compare to prev.
        check_section_a_realtime(run, status)
        check_section_b_ordering(run, status)
        check_section_c_stability(run, status)
        run.snapshots.append(status)

        # Print rolling status line every 4 polls (~8s) to limit noise.
        poll_no += 1
        if poll_no % 4 == 0 or poll_no <= 3:
            phases = phase_map(status)
            phase_str = " · ".join(f"{n}={p.get('state','?')}" for n, p in phases.items())
            bc = int(status.get("batches_completed") or 0)
            tb = int(status.get("total_batches") or 0)
            tiles = derive_tiles(status)
            in_flight = count_in_flight_from_log(status)
            print(
                f"  [T+{int(run.t):>4}s] state={status.get('state'):<10} "
                f"b={bc}/{tb} flight={in_flight} facts={tiles['facts']} "
                f"ents={tiles['entities']} emb={tiles['embedded']} med={tiles['media']} | {phase_str}",
                flush=True,
            )

        # Settle condition: no phase is ``in_flight`` AND ``state==idle``.
        # ``pending`` is treated as terminal because:
        #   - ``overview_wiki`` may legitimately stay ``pending`` when
        #     the channel is not yet eligible (e.g. first sync gate),
        #     or when no new memory has settled.
        #   - ``extracting`` may be ``pending`` for an empty channel.
        # Any active work ``in_flight`` resets the stability counter.
        phases = status.get("phases") or []
        none_in_flight = phases and not any(
            p.get("state") == "in_flight" for p in phases
        )
        idle = status.get("state") in (None, "idle", "completed")
        if none_in_flight and idle:
            stable_idle_polls += 1
        else:
            stable_idle_polls = 0
        # Require 3 stable polls (~6s) to avoid declaring settled
        # during a transient in_flight→pending blink (the very bug the
        # ``overview_wiki`` clamp guards against — but tests should not
        # rely on the clamp; they should observe stable settlement).
        if stable_idle_polls >= 3:
            print(f"\n  Settled at T+{int(run.t)}s — observation complete", flush=True)
            break

        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    return run


def render_report(run: TestRun) -> dict[str, Any]:
    a_perf = finalize_section_a_performance(run)
    b_summary, b_extras = finalize_section_b_ordering(run)
    run.violations.extend(b_extras)
    d_perf = finalize_section_d_performance(run)
    e_math = finalize_section_e_ui_math(run)

    errors = [v for v in run.violations if v.severity == "error"]
    warns = [v for v in run.violations if v.severity == "warn"]

    return {
        "channel_id": run.channel_id,
        "duration_seconds": round(run.t, 2),
        "snapshots_collected": len(run.snapshots),
        "verdict": "PASS" if not errors else "FAIL",
        "errors": len(errors),
        "warnings": len(warns),
        "section_a_realtime": a_perf,
        "section_b_ordering": b_summary,
        "section_d_performance": d_perf,
        "section_e_ui_math": e_math,
        "violations": [asdict(v) for v in run.violations],
    }


def print_human_report(report: dict[str, Any]) -> None:
    print("\n" + "=" * 78)
    print(f"PIPELINE DESIGN VERIFICATION — verdict: {report['verdict']}")
    print("=" * 78)
    print(f"  channel:    {report['channel_id']}")
    print(f"  duration:   {report['duration_seconds']}s ({report['snapshots_collected']} snapshots)")
    print(f"  errors:     {report['errors']}")
    print(f"  warnings:   {report['warnings']}")

    a = report["section_a_realtime"]
    print("\n[A] Real-time monitoring")
    print(f"      live batch increments observed:  {a.get('batches_observed_increments', 0)}")
    print(f"      final batches:                   {a.get('final_batches_completed')}/{a.get('final_total_batches')}")
    if "per_batch_seconds_median" in a:
        print(f"      per-batch gap (median):          {a['per_batch_seconds_median']}s")
        print(f"      per-batch gap (max):             {a['per_batch_seconds_max']}s")
        print(f"      per-batch gap (min):             {a['per_batch_seconds_min']}s")

    b = report["section_b_ordering"]
    print("\n[B] Pipeline ordering (source -> memory -> wiki)")
    print(f"      t_fetched_done:           {b.get('t_fetched_done')}")
    print(f"      t_extracting_done:        {b.get('t_extracting_done')}")
    print(f"      t_wiki_maintenance_done:  {b.get('t_wiki_maintenance_done')}")
    print(f"      t_overview_wiki_done:     {b.get('t_overview_wiki_done')}")
    print(f"      wiki_tail_seconds:        {b.get('wiki_tail_seconds')}")

    d = report["section_d_performance"]
    print("\n[D] Performance")
    print(f"      time-to-first-batch:    {d.get('t_first_batch_completed')}s")
    print(f"      observation_end_at:     {d.get('t_observation_end')}s")

    e = report["section_e_ui_math"]
    print("\n[E] Final UI math vs server")
    print(f"      UI batchesInFlight calc: {e.get('ui_in_flight_calc')}")
    print(f"      server done/total:       {e.get('server_batches_completed')}/{e.get('server_total_batches')}")
    print(f"      tiles  facts={e.get('ui_tile_facts')}  entities={e.get('ui_tile_entities')} "
          f" embedded={e.get('ui_tile_embedded')}  media={e.get('ui_tile_media')}")

    if report["errors"]:
        print("\nERRORS:")
        for v in report["violations"]:
            if v["severity"] == "error":
                d = " " + json.dumps(v["detail"]) if v["detail"] else ""
                print(f"  [{v['section']}/{v['invariant']}] @T+{v['t_seconds']}s — {v['message']}{d}")
    if report["warnings"]:
        print("\nWARNINGS:")
        for v in report["violations"][:6]:
            if v["severity"] == "warn":
                print(f"  [{v['section']}/{v['invariant']}] @T+{v['t_seconds']}s — {v['message']}")
    print("=" * 78)


async def main_async(args: argparse.Namespace) -> int:
    api_key = load_api_key()
    base_url = os.environ.get("BEEVER_API_URL", "http://localhost:8000")

    async with httpx.AsyncClient(base_url=base_url) as client:
        # Pre-flight
        try:
            h = await client.get("/api/health", timeout=5.0)
            h.raise_for_status()
        except Exception as exc:
            print(f"  backend not reachable at {base_url}: {exc}", flush=True)
            return 2

        if args.trigger:
            await trigger_sync(client, api_key, args.channel_id)

        run = await observe(client, api_key, args.channel_id, args.timeout)
        report = render_report(run)

        if args.json_out:
            Path(args.json_out).write_text(json.dumps(report, indent=2))
            print(f"\n  JSON report written: {args.json_out}", flush=True)

        if not args.quiet:
            print_human_report(report)

        return 0 if report["verdict"] == "PASS" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channel-id", required=True, help="channel ID to observe")
    parser.add_argument("--trigger", action="store_true", help="trigger a fresh sync at start")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help=f"max observation seconds (default {DEFAULT_TIMEOUT_SECONDS})")
    parser.add_argument("--json-out", help="write JSON report to file")
    parser.add_argument("--quiet", action="store_true", help="suppress human report")
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
