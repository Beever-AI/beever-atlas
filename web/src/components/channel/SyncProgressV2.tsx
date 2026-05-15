/**
 * SyncProgressV2 — phase-aware single-card monitor with tabbed body.
 *
 * Layout:
 *
 *   ┌────────────────────────────────────────────────────────────┐
 *   │ (●)─Fetch ── (●)─Extract ── (○)─Wiki ── (○)─Done           │  PipelineStepper
 *   ├────────────────────────────────────────────────────────────┤
 *   │ (spinner) Extracting facts    142/711 · 20%   ETA ~3 min   │  ProgressHeader
 *   │ [============>                                            ] │
 *   ├────────────────────────────────────────────────────────────┤
 *   │ [Pipeline Activity] [Batch Results]            View history│  Tabs + history link
 *   ├────────────────────────────────────────────────────────────┤
 *   │ Step 1/6 — Preprocessing messages                          │
 *   │ [📥] PREPROCESSOR  Batch 2                            0ms │  rich step cards
 *   │ Retained 12 messages · 2 media · 12 coref · 4 threads...   │  via ActivityLog
 *   │ ...                                                        │
 *   │ Step 2/6 — Extracting facts (LLM)                          │
 *   │ [🧠] FACT EXTRACTOR  Batch 2  gemini-2.5-flash      1.5s │
 *   │ Extracted 20 facts (avg quality 0.91)                      │
 *   │ ...                                                        │
 *   ├────────────────────────────────────────────────────────────┤
 *   │ Throughput: 12 msg/min · Elapsed 4:32 · LLM ~$0.04         │  footer
 *   └────────────────────────────────────────────────────────────┘
 *
 * Phase derivation, dedup, and adaptive polling are unchanged. This is
 * the BODY redesign — the rich per-step rendering comes from the
 * existing ``ActivityLog`` component in PipelineActivity.tsx, which
 * reads ``stage_details.activity_log`` directly. The new event taxonomy
 * (agent_state, wiki_update, cost_summary, parse_failure) flows into a
 * compact "Live Events" stream beneath the rich log so all backends are
 * covered.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  FileText,
  Loader2,
  Search,
  Sparkles,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type {
  ActivityEntry,
  BatchResultEntry,
  ParseFailureState,
  Phase,
  PhaseName,
  RecentEvent,
} from "@/lib/types";
import { ActivityLog } from "./PipelineActivity";
import { BatchResults } from "./SyncProgress";

// ─────────────────────────────────────────────────────────────────────────
// Phase model (unchanged from prior version)
// ─────────────────────────────────────────────────────────────────────────

type ActivePhase = "syncing" | "extracting" | "building" | "done" | "error";

interface SyncProgressV2Props {
  channelId: string;
  phases: Phase[];
  state: "idle" | "syncing" | "error";
  events: RecentEvent[];
  stageDetails?: {
    activity_log?: ActivityEntry[];
    batch_stages?: Record<string, string>;
    [key: string]: unknown;
  };
  batchResults?: BatchResultEntry[];
  /** The ``job_id`` that the current ``batchResults`` array came from.
   *  Threaded through so the consumer can gate ingestion against the
   *  ``currentJobId`` — without this, the brief window between
   *  triggering a new sync and the new ``sync_jobs`` row landing leaks
   *  the previous run's done chips into the chip strip. */
  batchResultsJobId?: string | null;
  /** The ``job_id`` of the current sync as known to the caller. Used to
   *  reset sticky accumulators and gate ``batchResults`` ingestion. */
  currentJobId?: string | null;
  smoothedEtaSeconds?: number | null;
  parseFailureState?: ParseFailureState | null;
  totalMessages?: number;
  processedMessages?: number;
  totalBatches?: number;
  batchesCompleted?: number;
  startedAt?: string | null;
  retrying?: number;
  abandoned?: number;
  /** When provided, makes the collapse toggle controlled by the parent.
   *  Allows the workspace layout to react to the collapsed state (e.g.
   *  switch from fullscreen monitor to a compact strip with the wiki
   *  body visible below). When undefined, the component keeps an
   *  internal localStorage-backed collapse state as before. */
  collapsed?: boolean;
  onCollapsedChange?: (next: boolean) => void;
}

const PHASE_DISPLAY_ORDER: Array<{ name: PhaseName; label: string }> = [
  { name: "fetched", label: "Fetch" },
  { name: "extracting", label: "Extract" },
  { name: "wiki_maintenance", label: "Wiki" },
  { name: "overview_wiki", label: "Done" },
];

// ───────────────────────────────────────────────────────────────────────
// Tiny localStorage-backed state hook.
// Persists across page navigations and reloads so the monitor remembers
// the user's last collapse / filter choices.
// ───────────────────────────────────────────────────────────────────────
function useLocalStorageState<T>(
  key: string,
  initialValue: T,
): [T, (value: T | ((prev: T) => T)) => void] {
  const [value, setValueRaw] = useState<T>(() => {
    if (typeof window === "undefined") return initialValue;
    try {
      const raw = window.localStorage.getItem(key);
      if (raw === null) return initialValue;
      return JSON.parse(raw) as T;
    } catch {
      return initialValue;
    }
  });
  const setValue = (next: T | ((prev: T) => T)) => {
    setValueRaw((prev) => {
      const resolved =
        typeof next === "function" ? (next as (p: T) => T)(prev) : next;
      try {
        window.localStorage.setItem(key, JSON.stringify(resolved));
      } catch {
        // Quota exceeded or storage disabled — degrade silently.
      }
      return resolved;
    });
  };
  return [value, setValue];
}

function deriveActivePhase(
  state: "idle" | "syncing" | "error",
  phases: Phase[],
): ActivePhase {
  if (state === "error") return "error";
  const byName = (n: PhaseName) => phases.find((p) => p.name === n);
  if (phases.some((p) => p.state === "failed")) return "error";
  // Phases waterfall is the single source of truth — the previous
  // ``|| state === "syncing"`` shortcut caused the header to say
  // "Fetching messages" forever, even after fetch had completed and
  // extraction was in flight. Walk the phases in order and return the
  // first one that's actually ``in_flight``.
  if (byName("fetched")?.state === "in_flight") return "syncing";
  if (byName("extracting")?.state === "in_flight") return "extracting";
  if (
    byName("wiki_maintenance")?.state === "in_flight" ||
    byName("overview_wiki")?.state === "in_flight"
  ) {
    return "building";
  }
  // Fallback: if the API still says ``syncing`` but no phase is
  // in_flight yet (sub-second window between fetch start and the first
  // status poll), assume we're in the fetch phase.
  if (state === "syncing") return "syncing";
  return "done";
}

const PHASE_LABELS: Record<ActivePhase, string> = {
  syncing: "Fetching messages",
  extracting: "Extracting facts",
  building: "Building wiki",
  done: "Pipeline complete",
  error: "Pipeline failed",
};

// ─────────────────────────────────────────────────────────────────────────
// Utility helpers
// ─────────────────────────────────────────────────────────────────────────

function fmtElapsed(fromIso: string | null | undefined, toMs: number): string {
  if (!fromIso) return "—";
  try {
    const start = new Date(fromIso).getTime();
    const diffSec = Math.max(0, Math.floor((toMs - start) / 1000));
    const min = Math.floor(diffSec / 60);
    const sec = diffSec % 60;
    return `${min}:${sec.toString().padStart(2, "0")}`;
  } catch {
    return "—";
  }
}

function fmtEta(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return "Calculating…";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const min = Math.round(seconds / 60);
  if (min < 60) return `~${min} min`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `~${h}h ${m}m`;
}

// ─────────────────────────────────────────────────────────────────────────
// PipelineStepper (unchanged)
// ─────────────────────────────────────────────────────────────────────────

interface StepperDotProps {
  state: "pending" | "active" | "done" | "failed";
  label: string;
  isLast?: boolean;
}

function StepperDot({ state, label, isLast }: StepperDotProps) {
  return (
    <div className="flex items-center gap-2 flex-1 min-w-0">
      <div className="flex flex-col items-center gap-1 shrink-0 relative">
        {/* Outer ripple ring — visible only when active. Heavier than the
         *  built-in ``animate-pulse`` so the user can see the dot is
         *  actually doing work, not just static art. */}
        {state === "active" && (
          <span
            className="absolute -inset-2 rounded-full bg-primary/20 animate-ping"
            style={{ animationDuration: "1.6s" }}
            aria-hidden="true"
          />
        )}
        <div
          className={cn(
            "w-3 h-3 rounded-full transition-colors duration-300 shrink-0 relative",
            state === "active" && "bg-primary ring-2 ring-primary/40",
            state === "done" && "bg-emerald-500",
            state === "failed" && "bg-red-500",
            state === "pending" && "bg-muted-foreground/30",
          )}
        />
        <span
          className={cn(
            "text-[10px] uppercase tracking-wide whitespace-nowrap",
            state === "active" && "text-primary font-semibold",
            state === "done" && "text-emerald-600 dark:text-emerald-400",
            state === "failed" && "text-red-500",
            state === "pending" && "text-muted-foreground/60",
          )}
        >
          {label}
        </span>
      </div>
      {!isLast && (
        <div
          className={cn(
            "flex-1 h-px transition-colors duration-300 min-w-[20px] relative overflow-hidden",
            state === "done"
              ? "bg-emerald-500/40"
              : "bg-muted-foreground/20",
          )}
        >
          {/* Active stepper segment — moving shimmer to signal "in motion".
           *  We approximate the look using two overlaid gradients on the
           *  next-stage segment when the current is active. */}
          {state === "active" && (
            <span
              aria-hidden="true"
              className="absolute inset-0 bg-gradient-to-r from-transparent via-primary/60 to-transparent animate-stepper-shimmer"
            />
          )}
        </div>
      )}
    </div>
  );
}

function PipelineStepper({
  phases,
  activePhase,
}: {
  phases: Phase[];
  activePhase: ActivePhase;
}) {
  const dotState = (
    name: PhaseName,
  ): "pending" | "active" | "done" | "failed" => {
    const p = phases.find((ph) => ph.name === name);
    if (p?.state === "done" || p?.state === "skipped") return "done";
    if (p?.state === "failed") return "failed";
    if (
      (activePhase === "syncing" && name === "fetched") ||
      (activePhase === "extracting" && name === "extracting") ||
      (activePhase === "building" &&
        (name === "wiki_maintenance" || name === "overview_wiki"))
    ) {
      return "active";
    }
    if (activePhase === "done") return "done";
    return "pending";
  };

  return (
    <div className="flex items-start gap-2 px-3 py-2.5">
      {PHASE_DISPLAY_ORDER.map((p, i) => (
        <StepperDot
          key={p.name}
          state={dotState(p.name)}
          label={p.label}
          isLast={i === PHASE_DISPLAY_ORDER.length - 1}
        />
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// ProgressHeader (unchanged)
// ─────────────────────────────────────────────────────────────────────────

function ProgressHeader({
  activePhase,
  totalMessages,
  processedMessages,
  smoothedEtaSeconds,
  startedAt,
  phases,
}: {
  activePhase: ActivePhase;
  totalMessages?: number;
  processedMessages?: number;
  smoothedEtaSeconds?: number | null;
  startedAt?: string | null;
  phases: Phase[];
}) {
  const Icon =
    activePhase === "done"
      ? CheckCircle2
      : activePhase === "error"
        ? AlertTriangle
        : Loader2;
  const iconClass =
    activePhase === "done"
      ? "text-emerald-500"
      : activePhase === "error"
        ? "text-red-500"
        : "text-primary animate-spin";

  const wikiPhase = phases.find((p) => p.name === "wiki_maintenance");
  const useWikiNumbers =
    activePhase === "building" && (wikiPhase?.total ?? 0) > 0;
  const done = useWikiNumbers
    ? (wikiPhase?.done ?? 0)
    : (processedMessages ?? 0);
  const total = useWikiNumbers
    ? (wikiPhase?.total ?? 0)
    : (totalMessages ?? 0);
  const unit = useWikiNumbers ? "pages" : "messages";
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  const elapsed = useMemo(
    () => (startedAt ? fmtElapsed(startedAt, Date.now()) : null),
    [startedAt],
  );

  return (
    <div className="border-y border-border bg-card px-3 py-2">
      <div className="flex items-center gap-2 flex-wrap">
        <Icon size={14} className={cn(iconClass, "shrink-0")} />
        <span className="text-sm font-semibold text-foreground">
          {PHASE_LABELS[activePhase]}
        </span>
        <span className="text-xs text-muted-foreground">
          <span className="font-medium text-foreground">{done}</span>
          <span className="mx-1 text-muted-foreground/60">/</span>
          <span>{total}</span>
          <span className="ml-1">{unit}</span>
          {pct > 0 && (
            <span className="ml-2 text-muted-foreground/70">· {pct}%</span>
          )}
        </span>
        <span className="ml-auto text-xs text-muted-foreground">
          {activePhase !== "done" &&
            activePhase !== "error" &&
            smoothedEtaSeconds != null &&
            smoothedEtaSeconds >= 0 && (
              <span>ETA {fmtEta(smoothedEtaSeconds)}</span>
            )}
          {(activePhase === "done" || activePhase === "error") && elapsed && (
            <span>Elapsed {elapsed}</span>
          )}
        </span>
      </div>
      {total > 0 && (
        <div className="mt-2 h-1 rounded-full bg-muted/60 overflow-hidden">
          <div
            className={cn(
              "h-full rounded-full transition-all duration-700 ease-out",
              activePhase === "done"
                ? "bg-emerald-500"
                : activePhase === "error"
                  ? "bg-red-500"
                  : "bg-primary",
            )}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// MetricsBar — derives rich monitoring counts from events + activity_log
// ─────────────────────────────────────────────────────────────────────────

export interface PipelineMetrics {
  totalBatches: number;
  batchesDone: number;
  batchesInFlight: number;
  totalFacts: number;
  totalEntities: number;
  totalRelationships: number;
  totalEmbedded: number;
  totalMediaEnriched: number;
}

export function deriveMetrics(
  events: RecentEvent[],
  activityLog: ActivityEntry[],
  stickyResults: BatchResultEntry[],
  jobTotalBatches?: number,
  jobBatchesCompleted?: number,
): PipelineMetrics {
  // Total + done counts come from the user-facing sync_jobs row, but
  // SyncRunner's ``total_batches`` is just an ESTIMATE based on a fixed
  // batch size, while ExtractionWorker actually produces a different
  // (usually larger) number of token-aware batches per tick. So the
  // ``done`` counter can legitimately exceed the static ``total``,
  // producing weird displays like "21/15". Clamp the denominator to be
  // at least ``done`` so the fraction stays sensible monotonically:
  // 5/15 → 9/15 → 14/15 → 21/21.
  const maxBatchIdxSeen = activityLog.reduce(
    (m, e) => Math.max(m, e.batch_idx ?? 0),
    0,
  );
  const batchesDone = jobBatchesCompleted ?? 0;
  const totalBatches = Math.max(
    jobTotalBatches ?? 0,
    batchesDone,
    maxBatchIdxSeen,
  );

  // In-flight: count batches that have a ``stage_start`` event but no
  // ``persister`` ``stage_output`` event yet — this matches what the
  // batch chip strip actually shows as ●Running. Previously this was
  // ``maxBatchIdxSeen - batchesDone`` which double-counted batches
  // whose persister entries had been evicted from the activity_log.
  const startedSet = new Set<number>();
  const persistedSet = new Set<number>();
  for (const e of activityLog) {
    if (e.batch_idx == null) continue;
    if (e.type === "stage_start") startedSet.add(e.batch_idx);
    else if (e.type === "stage_output" && e.agent === "persister") {
      persistedSet.add(e.batch_idx);
    }
  }
  const batchesInFlight = Array.from(startedSet).filter(
    (i) => !persistedSet.has(i),
  ).length;

  // Aggregate fact / entity / embedded / media counts from the STICKY
  // batch-results accumulator (kept by SyncProgressV2 across polls).
  // The previous implementation summed across raw ``activity_log`` —
  // but that log is server-side ``$slice``-capped, so as the sync
  // progresses, old stage_outputs evict and the tile counters
  // appeared to DROP. Sticky aggregation keeps the totals monotonic
  // for the entire sync session.
  let totalFacts = 0;
  let totalEntities = 0;
  let totalRelationships = 0;
  let totalEmbedded = 0;
  let totalMediaEnriched = 0;
  for (const r of stickyResults) {
    totalFacts += r.facts_count;
    totalEntities += r.entities_count;
    totalRelationships += r.relationships_count;
    totalEmbedded += r.embedded_count ?? 0;
    totalMediaEnriched += r.media_count ?? 0;
  }

  // Also count message_processing events from the recent_events ring
  // as a fallback metric source (when activity_log is empty during
  // the warm-up window).
  if (totalFacts === 0 && totalEntities === 0) {
    // Fallback signal: if nothing in activity_log yet, surface
    // message_processing counts so the user sees activity.
    const processingCount = events.filter(
      (e) => e.event_type === "message_processing",
    ).length;
    return {
      totalBatches,
      batchesDone,
      batchesInFlight: Math.max(batchesInFlight, processingCount > 0 ? 1 : 0),
      totalFacts: 0,
      totalEntities: 0,
      totalRelationships: 0,
      totalEmbedded: 0,
      totalMediaEnriched: 0,
    };
  }

  return {
    totalBatches,
    batchesDone,
    batchesInFlight,
    totalFacts,
    totalEntities,
    totalRelationships,
    totalEmbedded,
    totalMediaEnriched,
  };
}

interface MetricBadgeProps {
  label: string;
  value: number | string;
  detail?: string;
  accent?: "default" | "primary" | "emerald" | "violet" | "amber" | "sky";
}

function MetricBadge({ label, value, detail, accent = "default" }: MetricBadgeProps) {
  const accentClasses: Record<NonNullable<MetricBadgeProps["accent"]>, string> = {
    default: "text-foreground",
    primary: "text-primary",
    emerald: "text-emerald-500",
    violet: "text-violet-500",
    amber: "text-amber-500",
    sky: "text-sky-500",
  };
  // Detect when the displayed value changes from the previous render so
  // we can flash a brief highlight on the tile — gives the user a
  // visual cue that a metric just updated, instead of numbers silently
  // ticking up.
  const prevValueRef = useRef<number | string | null>(null);
  const [flashKey, setFlashKey] = useState(0);
  useEffect(() => {
    if (prevValueRef.current !== null && prevValueRef.current !== value) {
      setFlashKey((k) => k + 1);
    }
    prevValueRef.current = value;
  }, [value]);
  return (
    <div className="flex flex-col gap-0.5 min-w-0 px-1.5 py-0.5 -mx-1.5 -my-0.5 rounded">
      <div className="text-[9px] uppercase tracking-wider text-muted-foreground/60 font-medium">
        {label}
      </div>
      <div
        key={flashKey}
        className="flex items-baseline gap-1 motion-safe:animate-value-flash"
      >
        <span className={cn("text-sm font-semibold tabular-nums", accentClasses[accent])}>
          {value}
        </span>
        {detail && (
          <span className="text-[10px] text-muted-foreground/70 font-mono">
            {detail}
          </span>
        )}
      </div>
    </div>
  );
}

export function MetricsBar({
  events,
  activityLog,
  stickyResults,
  totalMessages,
  processedMessages,
  totalBatches,
  batchesCompleted,
}: {
  events: RecentEvent[];
  activityLog: ActivityEntry[];
  stickyResults: BatchResultEntry[];
  totalMessages?: number;
  processedMessages?: number;
  totalBatches?: number;
  batchesCompleted?: number;
}) {
  const m = useMemo(
    () =>
      deriveMetrics(
        events,
        activityLog,
        stickyResults,
        totalBatches,
        batchesCompleted,
      ),
    [events, activityLog, stickyResults, totalBatches, batchesCompleted],
  );

  const msgsDone = processedMessages ?? 0;
  const msgsTotal = totalMessages ?? 0;
  const msgsRemaining = Math.max(0, msgsTotal - msgsDone);

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 border-y border-border bg-muted/10 px-3 py-2">
      <MetricBadge
        label="Messages"
        value={`${msgsDone}/${msgsTotal}`}
        detail={msgsRemaining > 0 ? `${msgsRemaining} left` : "complete"}
        accent={msgsRemaining > 0 ? "primary" : "emerald"}
      />
      <MetricBadge
        label="Batches"
        value={m.totalBatches > 0 ? `${m.batchesDone}/${m.totalBatches}` : "—"}
        detail={m.batchesInFlight > 0 ? `${m.batchesInFlight} active` : undefined}
        accent={m.batchesInFlight > 0 ? "primary" : "emerald"}
      />
      <MetricBadge
        label="Facts"
        value={m.totalFacts}
        accent="violet"
      />
      <MetricBadge
        label="Entities"
        value={m.totalEntities}
        detail={m.totalRelationships > 0 ? `${m.totalRelationships} rels` : undefined}
        accent="emerald"
      />
      <MetricBadge
        label="Embedded"
        value={m.totalEmbedded}
        accent="amber"
      />
      <MetricBadge
        label="Media"
        value={m.totalMediaEnriched}
        accent="sky"
      />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// BatchFilteredActivityLog — per-batch tabs that filter the activity log
// ─────────────────────────────────────────────────────────────────────────

export interface BatchSummary {
  batchIdx: number;
  state: "pending" | "running" | "done" | "failed";
  stagesStarted: number;
  hasPersisterDone: boolean;
  factsCount: number;
  entitiesCount: number;
  totalElapsedMs: number;
  hasFailure: boolean;
}

export function summariseBatches(
  activityLog: ActivityEntry[],
  totalBatches?: number,
  batchesCompleted?: number,
  /**
   * Set of batch_nums that the BACKEND has confirmed done (from
   * the user-facing ``batch_results`` array). When batches complete
   * out of order (e.g. Batch 5 finishes before Batch 1), the prior
   * ``idx <= batchesCompleted`` heuristic mis-marked early indices
   * as done. Now we trust this explicit set instead.
   */
  knownDoneBatchNums?: Set<number>,
): BatchSummary[] {
  const byBatch = new Map<number, BatchSummary>();
  for (const e of activityLog) {
    if (e.batch_idx == null) continue;
    const idx = e.batch_idx;
    if (!byBatch.has(idx)) {
      byBatch.set(idx, {
        batchIdx: idx,
        state: "pending",
        stagesStarted: 0,
        hasPersisterDone: false,
        factsCount: 0,
        entitiesCount: 0,
        totalElapsedMs: 0,
        hasFailure: false,
      });
    }
    const s = byBatch.get(idx)!;
    if (e.type === "stage_start") {
      s.stagesStarted += 1;
    } else if (e.type === "stage_output") {
      if (e.agent === "persister") s.hasPersisterDone = true;
      if (e.agent === "fact_extractor")
        s.factsCount += Number(e.metrics?.count ?? 0);
      if (e.agent === "entity_extractor")
        s.entitiesCount += Number(e.metrics?.entities ?? 0);
      if (typeof e.elapsed === "number") s.totalElapsedMs += e.elapsed * 1000;
    }
  }
  // Derive state per batch. Consult ``knownDoneBatchNums`` as the
  // authoritative done signal — it survives activity_log $slice
  // eviction. Without this override, a batch that has some events
  // in the log (e.g. stage_start) but whose persister event has
  // scrolled off would render as "running" forever, even though
  // batch_results.json confirms it's done.
  for (const s of byBatch.values()) {
    if (s.hasFailure) {
      s.state = "failed";
      continue;
    }
    if (s.hasPersisterDone || knownDoneBatchNums?.has(s.batchIdx)) {
      s.state = "done";
      s.hasPersisterDone = true;
      continue;
    }
    if (s.stagesStarted > 0) s.state = "running";
    else s.state = "pending";
  }

  // Always render the full strip 1..totalBatches so the user sees every
  // batch that will run, even before activity_log entries arrive for it.
  // ``batchesCompleted`` is the user-facing row's global counter
  // (incremented once per tick by ExtractionWorker), so batches
  // 1..batchesCompleted are definitively done — anything higher that we
  // haven't yet observed is pending.
  if (totalBatches && totalBatches > 0) {
    // Use the explicit done-set when available; fall back to the
    // legacy idx-based rule only when knownDoneBatchNums is missing
    // entirely (legacy backend / pre-result-row state). The legacy
    // rule wrongly marks batches 1..N as done when in reality the N
    // completed batches may be {2, 5, 7} (out-of-order processing).
    // UI testing caught this as "Batch 1 said done, then flipped to
    // running when real preprocessor events arrived."
    const useExplicit = knownDoneBatchNums !== undefined;
    const done = Math.max(0, batchesCompleted ?? 0);
    for (let i = 1; i <= totalBatches; i++) {
      if (byBatch.has(i)) continue;
      const isDone = useExplicit
        ? knownDoneBatchNums.has(i)
        : i <= done;
      byBatch.set(i, {
        batchIdx: i,
        state: isDone ? "done" : "pending",
        stagesStarted: 0,
        hasPersisterDone: isDone,
        factsCount: 0,
        entitiesCount: 0,
        totalElapsedMs: 0,
        hasFailure: false,
      });
    }
  }

  return Array.from(byBatch.values()).sort((a, b) => a.batchIdx - b.batchIdx);
}

// ──────────────────────────────────────────────────────────────────────
// deriveBatchResultsFromActivity — synthesise ``BatchResultEntry[]`` from
// the activity_log so the Batch Results tab is populated under the
// decoupled ExtractionWorker flow (where ``sync_jobs.batch_results``
// stays empty because the worker uses synthetic job_ids).
// ──────────────────────────────────────────────────────────────────────
export function deriveBatchResultsFromActivity(
  activityLog: ActivityEntry[],
): BatchResultEntry[] {
  const byBatch = new Map<number, BatchResultEntry>();
  for (const e of activityLog) {
    if (e.batch_idx == null) continue;
    const idx = e.batch_idx;
    if (!byBatch.has(idx)) {
      byBatch.set(idx, {
        batch_num: idx,
        facts_count: 0,
        entities_count: 0,
        relationships_count: 0,
        embedded_count: 0,
        media_count: 0,
        sample_facts: [],
        sample_entities: [],
        sample_relationships: [],
        duration_seconds: 0,
        error: null,
      });
    }
    const acc = byBatch.get(idx)!;
    if (e.type !== "stage_output") continue;
    if (typeof e.elapsed === "number") acc.duration_seconds += e.elapsed;
    const m = e.metrics ?? {};
    if (e.agent === "fact_extractor") {
      acc.facts_count += Number(m.count ?? 0);
      for (const s of e.samples ?? []) {
        if (s.item_type === "fact" && acc.sample_facts.length < 8) {
          const c = (s.content ?? "").trim();
          if (c) acc.sample_facts.push(c);
        }
      }
    } else if (e.agent === "entity_extractor") {
      acc.entities_count += Number(m.entities ?? 0);
      acc.relationships_count += Number(m.relationships ?? 0);
      for (const s of e.samples ?? []) {
        if (s.item_type === "entity" && acc.sample_entities.length < 12) {
          const name = s.content ?? "";
          const type = (s.tags ?? [])[0] ?? "";
          if (name) acc.sample_entities.push({ name, type });
        } else if (
          s.item_type === "relationship" &&
          acc.sample_relationships.length < 8
        ) {
          acc.sample_relationships.push({
            source: s.source ?? "",
            target: s.target ?? "",
            type: s.rel_type ?? "",
          });
        }
      }
    } else if (e.agent === "embedder") {
      acc.embedded_count = (acc.embedded_count ?? 0) + Number(m.embedded ?? 0);
    } else if (e.agent === "preprocessor") {
      acc.media_count = (acc.media_count ?? 0) + Number(m.media ?? m.media_enriched ?? 0);
    }
  }
  return Array.from(byBatch.values()).sort(
    (a, b) => a.batch_num - b.batch_num,
  );
}

type BatchStateFilter = "all" | "done" | "running" | "pending" | "failed";

export function BatchTabs({
  batches,
  selected,
  onSelect,
}: {
  batches: BatchSummary[];
  selected: number | "all";
  onSelect: (sel: number | "all") => void;
}) {
  const [stateFilter, setStateFilter] =
    useLocalStorageState<BatchStateFilter>(
      "beever.monitor.activityStateFilter",
      "all",
    );

  if (batches.length === 0) return null;

  // Per-state counts for the top-row state filter chips.
  const counts = {
    all: batches.length,
    done: batches.filter((b) => b.state === "done").length,
    running: batches.filter((b) => b.state === "running").length,
    pending: batches.filter((b) => b.state === "pending").length,
    failed: batches.filter((b) => b.state === "failed").length,
  };

  const visibleBatches =
    stateFilter === "all"
      ? batches
      : batches.filter((b) => b.state === stateFilter);

  const STATE_CHIPS: Array<{
    key: BatchStateFilter;
    label: string;
    color: string;
    icon: string;
  }> = [
    { key: "all", label: "All", color: "text-foreground", icon: "·" },
    { key: "done", label: "Done", color: "text-emerald-500", icon: "✓" },
    { key: "running", label: "Running", color: "text-primary", icon: "●" },
    { key: "pending", label: "Pending", color: "text-muted-foreground/60", icon: "○" },
    { key: "failed", label: "Failed", color: "text-red-500", icon: "✗" },
  ];

  return (
    <div className="sticky top-0 z-20 border-b border-border bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/80">
      {/* Row 1 — State filter chips with live counts */}
      <div className="flex flex-wrap items-center gap-1 px-3 py-1.5 border-b border-border/40">
        {STATE_CHIPS.map((f) => {
          const count = counts[f.key];
          const active = stateFilter === f.key;
          const disabled = count === 0 && f.key !== "all";
          return (
            <button
              key={f.key}
              type="button"
              disabled={disabled}
              onClick={() => {
                setStateFilter(f.key);
                // Reset the per-batch selection when filter changes so
                // the user doesn't end up looking at a hidden batch.
                if (selected !== "all") {
                  const stillVisible = batches.find(
                    (b) => b.batchIdx === selected && (f.key === "all" || b.state === f.key),
                  );
                  if (!stillVisible) onSelect("all");
                }
              }}
              className={cn(
                "inline-flex items-center gap-1.5 px-2 py-0.5 text-[10px] rounded transition-colors",
                active
                  ? "bg-primary/10 text-primary border border-primary/20"
                  : "text-muted-foreground hover:text-foreground hover:bg-muted/40 border border-transparent",
                disabled && "opacity-40 cursor-not-allowed",
              )}
            >
              <span
                className={cn(
                  "font-mono",
                  f.color,
                  // Pulse animation on the "Running" dot icon so users
                  // notice when batches are actively processing.
                  f.key === "running" && count > 0 && "animate-pulse",
                )}
              >
                {f.icon}
              </span>
              <span className="font-medium uppercase tracking-wide">{f.label}</span>
              <span className="text-[9px] tabular-nums text-muted-foreground/70">
                {count}
              </span>
            </button>
          );
        })}
      </div>

      {/* Row 2 — individual batch chips, filtered by state */}
      <div className="flex flex-wrap items-center gap-1 px-3 py-1.5">
        <button
          type="button"
          onClick={() => onSelect("all")}
          className={cn(
            "px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide rounded transition-colors",
            selected === "all"
              ? "text-primary bg-primary/10"
              : "text-muted-foreground hover:text-foreground hover:bg-muted/40",
          )}
        >
          All ({visibleBatches.length})
        </button>
        <span className="text-muted-foreground/30">·</span>
        {visibleBatches.map((b) => {
          const isSelected = selected === b.batchIdx;
          const stateColor =
            b.state === "running"
              ? "text-primary"
              : b.state === "done"
                ? "text-emerald-500"
                : b.state === "failed"
                  ? "text-red-500"
                  : "text-muted-foreground/50";
          const stateIcon =
            b.state === "running"
              ? "●"
              : b.state === "done"
                ? "✓"
                : b.state === "failed"
                  ? "✗"
                  : "○";
          return (
            <button
              key={b.batchIdx}
              type="button"
              onClick={() => onSelect(b.batchIdx)}
              title={
                `Batch ${b.batchIdx} — ${b.state}` +
                (b.factsCount > 0 ? ` · ${b.factsCount} facts` : "") +
                (b.entitiesCount > 0 ? ` · ${b.entitiesCount} entities` : "") +
                (b.totalElapsedMs > 0
                  ? ` · ${(b.totalElapsedMs / 1000).toFixed(1)}s`
                  : "")
              }
              className={cn(
                "inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-mono rounded transition-all duration-300",
                isSelected
                  ? "text-primary bg-primary/10 border border-primary/20 scale-[1.04]"
                  : "text-muted-foreground hover:text-foreground hover:bg-muted/40",
                // Running batches get a subtle pulse so they "breathe"
                b.state === "running" && "ring-1 ring-primary/30 animate-pulse",
              )}
            >
              <span className={cn(stateColor, b.state === "running" && "animate-pulse")}>
                {stateIcon}
              </span>
              Batch {b.batchIdx}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Module-level activity-log cache. Survives across tab navigation
// (component unmount/remount) so the user doesn't lose the per-batch
// history when they hop between Wiki / Source / Settings tabs.
//
// Keyed by ``${channelId}::${startedAt}`` so a NEW sync starts with
// fresh state — without the startedAt segment, persister events from
// the PREVIOUS sync polluted the current sync's chip strip ("Batch 1
// DONE while its activity log shows preprocessor running").
interface _ActivityLogCacheEntry {
  startedAt: string | null;
  entries: Map<string, ActivityEntry>;
}
const _activityLogCache: Map<string, _ActivityLogCacheEntry> = new Map();
function _getActivityCacheFor(
  channelId: string,
  startedAt: string | null,
): Map<string, ActivityEntry> {
  const existing = _activityLogCache.get(channelId);
  if (existing && existing.startedAt === startedAt) {
    return existing.entries;
  }
  const fresh: _ActivityLogCacheEntry = {
    startedAt,
    entries: new Map<string, ActivityEntry>(),
  };
  _activityLogCache.set(channelId, fresh);
  return fresh.entries;
}

function BatchFilteredActivityLog({
  stageDetails,
  totalBatches,
  batchesCompleted,
  channelId,
  startedAt,
  knownDoneBatchNums,
}: {
  stageDetails?: {
    activity_log?: ActivityEntry[];
    [k: string]: unknown;
  };
  totalBatches?: number;
  batchesCompleted?: number;
  channelId?: string;
  /** Sync-identity timestamp — used to key the module-level activity
   *  log cache so the buffer naturally resets when a new sync starts
   *  (different ``started_at``) without manual cleanup. */
  startedAt?: string | null;
  /** Authoritative set of done batch_nums computed by the parent
   *  SyncProgressV2 from sticky_results + activity_log persister
   *  events. The activity_log buffer is server-side $sliced to the
   *  last 50 entries, so early-completed batches' persister events
   *  scroll off — without this prop the chip strip mis-reports them
   *  as still pending (caught by UI testing). */
  knownDoneBatchNums?: Set<number>;
}) {
  const [selectedBatch, setSelectedBatch] = useState<number | "all">("all");
  const [searchTerm, setSearchTerm] = useState<string>("");
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const rawActivityLog = stageDetails?.activity_log ?? [];

  // Sticky activity-entry accumulator — preserves the full per-batch
  // history across polls AND across tab navigation. Backed by a
  // module-level Map keyed by channelId so unmount/remount no longer
  // clears the buffer.
  //
  // Key by ``${batch_idx}:${type}:${agent}:${message_hash}`` — stable
  // identity for retries with the same agent (same key clobbers; OK
  // because the latest payload is the most accurate).
  const stickyMap = _getActivityCacheFor(channelId || "_default", startedAt ?? null);
  const activityLog = useMemo(() => {
    for (const entry of rawActivityLog) {
      const key =
        `${entry.batch_idx ?? "-"}:${entry.type}:${entry.agent}:` +
        `${(entry.message ?? "").slice(0, 80)}`;
      stickyMap.set(key, entry);
    }
    // Sort sticky entries: primary by batch_idx, secondary by a stable
    // order that approximates "stage progression". The activity_log
    // from the server is already roughly chronological — we preserve
    // insertion order within each batch via Map's preservation, then
    // group by batch.
    const all = Array.from(stickyMap.values());
    all.sort((a, b) => {
      const ai = a.batch_idx ?? Number.MAX_SAFE_INTEGER;
      const bi = b.batch_idx ?? Number.MAX_SAFE_INTEGER;
      return ai - bi;
    });
    return all;
  }, [rawActivityLog, stickyMap]);

  // Cmd-K / Ctrl-K: focus the activity-log search. Esc when focused: clear.
  // Mounted only on the Pipeline Activity tab, so won't collide with the
  // Batch Results panel's own Cmd-K.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        searchInputRef.current?.focus();
        searchInputRef.current?.select();
      } else if (
        e.key === "Escape" &&
        document.activeElement === searchInputRef.current
      ) {
        e.preventDefault();
        setSearchTerm("");
        searchInputRef.current?.blur();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const batches = useMemo(
    () => {
      // Confirmed-done set: union of (a) ``knownDoneBatchNums`` from
      // the parent (which combines sticky_results + persister events
      // — survives the activity_log $slice eviction) and (b) any
      // additional persister events in the local activity_log slice.
      // Either source proves the batch finished.
      const persistedNums = new Set<number>(knownDoneBatchNums ?? []);
      for (const e of activityLog) {
        if (
          e.type === "stage_output" &&
          e.agent === "persister" &&
          typeof e.batch_idx === "number"
        ) {
          persistedNums.add(e.batch_idx);
        }
      }
      return summariseBatches(activityLog, totalBatches, batchesCompleted, persistedNums);
    },
    [activityLog, totalBatches, batchesCompleted, knownDoneBatchNums],
  );

  // Filter pipeline:
  //   1. by selected batch chip (if not "all")
  //   2. by search term (case-insensitive match on agent / stage / message)
  const filteredEntries = useMemo(() => {
    let entries: ActivityEntry[] = activityLog;
    if (selectedBatch !== "all") {
      entries = entries.filter((e) => e.batch_idx === selectedBatch);
    }
    const q = searchTerm.trim().toLowerCase();
    if (q) {
      entries = entries.filter((e) => {
        const hay = [
          e.agent ?? "",
          e.stage ?? "",
          e.message ?? "",
          e.model ?? "",
        ]
          .join(" ")
          .toLowerCase();
        return hay.includes(q);
      });
    }
    return entries;
  }, [activityLog, selectedBatch, searchTerm]);

  const filteredDetails = useMemo(
    () => ({ ...stageDetails, activity_log: filteredEntries }),
    [stageDetails, filteredEntries],
  );

  // Contextual empty-state message — shown when the filter produces zero
  // entries. Replaces the old generic "Waiting for pipeline events..."
  // with something that tells the user WHY there's nothing to show.
  const emptyMessage = useMemo<{ title: string; hint: string } | null>(() => {
    if (filteredEntries.length > 0) return null;
    if (searchTerm.trim()) {
      return {
        title: `No matches for "${searchTerm.trim()}"`,
        hint: "Try a different keyword, or clear the search to see all events.",
      };
    }
    if (selectedBatch === "all") {
      return activityLog.length === 0
        ? {
            title: "Waiting for the first pipeline event…",
            hint: "The worker is queuing up — events will appear here as batches start processing.",
          }
        : {
            title: "No matching events",
            hint: "Try clicking a specific batch chip above.",
          };
    }
    const b = batches.find((x) => x.batchIdx === selectedBatch);
    if (!b) return { title: `Batch ${selectedBatch} not found`, hint: "" };
    switch (b.state) {
      case "running":
        return {
          title: `Batch ${b.batchIdx} is processing…`,
          hint: "Events stream in real time — first one usually appears within a few seconds.",
        };
      case "pending":
        return {
          title: `Batch ${b.batchIdx} is queued`,
          hint: "Waiting for a worker slot. The pipeline runs 4 batches in parallel by default.",
        };
      case "done":
        return {
          title: `Batch ${b.batchIdx} completed`,
          hint: "Detailed events have aged out of the log buffer. Open the Batch Results tab to see this batch's facts and entities.",
        };
      case "failed":
        return {
          title: `Batch ${b.batchIdx} failed`,
          hint: "Check the Batch Results tab for the error message.",
        };
    }
  }, [filteredEntries, searchTerm, selectedBatch, activityLog, batches]);

  // "Up next" preview: rendered at the bottom of the activity panel
  // when (a) viewing all batches and (b) there are pending batches —
  // so the panel fills available vertical space with useful content
  // instead of leaving a large empty gap during fullscreen mode.
  const pendingBatches = batches.filter((b) => b.state === "pending");
  const runningBatches = batches.filter((b) => b.state === "running");
  const showUpNext = selectedBatch === "all" && pendingBatches.length > 0;
  const allBatchesDone =
    batches.length > 0 && batches.every((b) => b.state === "done");

  return (
    <div className="min-h-full flex flex-col">
      {/* Header bundle: batch chips + search input.
       *  Sticky-pinned at the top of the scroll container so it stays
       *  visible during long activity-log scrolls. */}
      <div className="sticky top-0 z-20 bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/80">
      <BatchTabs
        batches={batches}
        selected={selectedBatch}
        onSelect={setSelectedBatch}
      />
      <div
        className="flex items-center gap-2 px-3 py-1.5 border-b border-border"
      >
        <Search size={12} className="text-muted-foreground/60 shrink-0" />
        <input
          ref={searchInputRef}
          type="search"
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          placeholder="Search events… (agent, stage, message) — ⌘K"
          className="flex-1 bg-transparent border-0 outline-none text-[11px] placeholder:text-muted-foreground/50 text-foreground"
        />
        {searchTerm && (
          <button
            type="button"
            onClick={() => setSearchTerm("")}
            className="text-[10px] text-muted-foreground hover:text-foreground"
          >
            clear
          </button>
        )}
        <span className="text-[10px] text-muted-foreground/60 tabular-nums">
          {filteredEntries.length} {filteredEntries.length === 1 ? "event" : "events"}
        </span>
      </div>
      </div>
      {emptyMessage ? (
        <div className="flex flex-col items-center justify-center py-8 px-4 text-center gap-1.5">
          <Loader2
            size={16}
            className={cn(
              "text-muted-foreground/40",
              // Only animate-spin when there's still active extraction
              selectedBatch !== "all" &&
                batches.find((x) => x.batchIdx === selectedBatch)?.state ===
                  "running" &&
                "animate-spin text-primary/60",
            )}
          />
          <div className="text-[12px] font-medium text-foreground/80">
            {emptyMessage.title}
          </div>
          {emptyMessage.hint && (
            <div className="text-[10.5px] text-muted-foreground/70 max-w-md">
              {emptyMessage.hint}
            </div>
          )}
        </div>
      ) : (
        <ActivityLog details={filteredDetails} />
      )}
      {showUpNext && (
        <UpNextStrip
          pending={pendingBatches}
          running={runningBatches}
        />
      )}
      {allBatchesDone && (
        <div className="mt-auto flex items-center justify-center gap-2 px-3 py-3 text-[11px] text-muted-foreground border-t border-border/40 bg-muted/10">
          <Loader2 size={12} className="animate-spin text-primary/60" />
          <span>
            All {batches.length} batches processed — finalising wiki…
          </span>
        </div>
      )}
      {/* Bottom-gap filler — when batches are RUNNING but none are pending
       *  (so UpNextStrip doesn't render) and not yet all done, render a
       *  centered "live updates" indicator pushed to the bottom via
       *  ``mt-auto`` so the activity panel doesn't show a big white gap
       *  in fullscreen mode. */}
      {!allBatchesDone && !showUpNext && runningBatches.length > 0 && (
        <div className="mt-auto flex items-center justify-center gap-2 px-3 py-3 text-[11px] text-muted-foreground/70 border-t border-border/30 bg-muted/5">
          <Loader2 size={12} className="animate-spin text-primary/50" />
          <span>
            {runningBatches.length} batch
            {runningBatches.length === 1 ? "" : "es"} running — live updates as events arrive
          </span>
        </div>
      )}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// UpNextStrip — fills the bottom of the activity panel with placeholder
// rows for pending/queued batches so fullscreen mode shows useful
// context (what's coming) instead of an empty gap.
// ──────────────────────────────────────────────────────────────────────
export function UpNextStrip({
  pending,
  running,
}: {
  pending: BatchSummary[];
  running: BatchSummary[];
}) {
  // Cap visible rows to avoid runaway height. Remaining count is summarised.
  const VISIBLE = 8;
  const visible = pending.slice(0, VISIBLE);
  const hiddenCount = Math.max(0, pending.length - VISIBLE);

  return (
    <div className="mt-auto border-t border-border/40 bg-muted/5 px-3 py-2.5">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10.5px] uppercase tracking-wider font-semibold text-muted-foreground/70">
          Up next · {pending.length} pending
          {running.length > 0 && ` · ${running.length} running`}
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-1.5">
        {visible.map((b) => (
          <div
            key={b.batchIdx}
            className="flex items-center gap-2 px-2 py-1.5 rounded-md border border-dashed border-border/40 bg-card/30 text-[11px] text-muted-foreground/80"
          >
            <span
              aria-hidden
              className="h-1.5 w-1.5 rounded-full bg-muted-foreground/40 shrink-0"
            />
            <span className="font-medium text-foreground/70">
              Batch {b.batchIdx}
            </span>
            <span className="ml-auto text-[10px] text-muted-foreground/60">
              queued
            </span>
          </div>
        ))}
        {hiddenCount > 0 && (
          <div className="flex items-center gap-2 px-2 py-1.5 rounded-md border border-dashed border-border/40 bg-card/20 text-[11px] text-muted-foreground/60">
            +{hiddenCount} more
          </div>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// CostSummaryBadge — aggregate LLM cost from cost_summary events
// ─────────────────────────────────────────────────────────────────────────

function CostSummaryBadge({ events }: { events: RecentEvent[] }) {
  const summary = useMemo(() => {
    let totalCalls = 0;
    let skippedCalls = 0;
    let durationMs = 0;
    for (const evt of events) {
      if (evt.event_type !== "cost_summary") continue;
      const p = evt.payload ?? {};
      totalCalls += Number(p.calls_total ?? 0);
      skippedCalls += Number(p.calls_skipped ?? 0);
      durationMs += Number(p.duration_ms ?? 0);
    }
    return { totalCalls, skippedCalls, durationMs };
  }, [events]);

  if (summary.totalCalls === 0 && summary.skippedCalls === 0) return null;

  return (
    <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
      <Sparkles size={11} className="text-amber-500" />
      <span>
        Builder: <span className="font-medium text-foreground">{summary.totalCalls}</span> LLM call
        {summary.totalCalls === 1 ? "" : "s"}
        {summary.skippedCalls > 0 && (
          <span className="text-muted-foreground/70">
            {" "}
            ({summary.skippedCalls} cached)
          </span>
        )}
      </span>
    </span>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Tabs
// ─────────────────────────────────────────────────────────────────────────

type TabId = "activity" | "batches";

function Tabs({
  active,
  onChange,
  batchCount,
  channelId,
}: {
  active: TabId;
  onChange: (t: TabId) => void;
  batchCount: number;
  channelId: string;
}) {
  return (
    <div className="flex items-center gap-1 border-b border-border bg-card px-2 py-1">
      <button
        type="button"
        onClick={() => onChange("activity")}
        className={cn(
          "px-2.5 py-1 text-[11px] font-medium uppercase tracking-wide rounded transition-colors",
          active === "activity"
            ? "text-primary bg-primary/10"
            : "text-muted-foreground hover:text-foreground hover:bg-muted/40",
        )}
      >
        Pipeline Activity
      </button>
      <button
        type="button"
        onClick={() => onChange("batches")}
        className={cn(
          "inline-flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium uppercase tracking-wide rounded transition-colors",
          active === "batches"
            ? "text-primary bg-primary/10"
            : "text-muted-foreground hover:text-foreground hover:bg-muted/40",
        )}
      >
        Batch Results
        {batchCount > 0 && (
          <span className="text-[10px] font-mono bg-muted/60 px-1 rounded">
            {batchCount}
          </span>
        )}
      </button>
      <Link
        to={`/channels/${channelId}/sync-history`}
        className="ml-auto inline-flex items-center gap-1 px-2.5 py-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
        title="See historical sync runs"
      >
        Sync history <ExternalLink size={10} />
      </Link>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// LiveEventsRow — compact row for the new event_type taxonomy
// (when stage_details.activity_log is empty — modern backends without
// the legacy stage_output emitters still produce these.)
// ─────────────────────────────────────────────────────────────────────────

function LiveEventsList({
  events,
  startedAt,
  maxRows = 8,
}: {
  events: RecentEvent[];
  startedAt?: string | null;
  maxRows?: number;
}) {
  const visible = useMemo(
    () =>
      events
        .filter(
          (e) =>
            e.event_type === "wiki_update" ||
            e.event_type === "cost_summary" ||
            e.event_type === "parse_failure",
        )
        .slice(0, maxRows),
    [events, maxRows],
  );

  if (visible.length === 0) return null;

  return (
    <div className="border-t border-border/50 px-3 py-2 bg-muted/10">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground font-semibold mb-1.5">
        Wiki & cost events
      </div>
      <ul className="space-y-1">
        {visible.map((evt, idx) => {
          const elapsed = (() => {
            try {
              return fmtElapsed(startedAt, new Date(evt.ts).getTime());
            } catch {
              return "—";
            }
          })();
          const payload = evt.payload ?? {};
          if (evt.event_type === "wiki_update") {
            const action = String(payload.action ?? "patched");
            const isSkipped = action === "skipped_frozen";
            const pageTitle = String(
              payload.page_title ?? payload.page_id ?? evt.label,
            );
            const facts = Number(payload.facts_integrated ?? 0);
            const version = payload.version;
            return (
              <li
                key={`${evt.ts}-${idx}`}
                className={cn(
                  "flex items-center gap-1.5 text-[11px] border-l-2 pl-2",
                  isSkipped ? "border-amber-400" : "border-violet-400",
                )}
              >
                <span className="font-mono text-[10px] text-muted-foreground tabular-nums w-9">
                  {elapsed}
                </span>
                <FileText
                  size={11}
                  className={cn(
                    isSkipped ? "text-amber-500" : "text-violet-500",
                  )}
                />
                <span className="text-foreground/85 truncate">
                  {isSkipped ? "Skipped (frozen)" : "Updated"} "{pageTitle}"
                </span>
                {facts > 0 && (
                  <span className="text-muted-foreground/70">
                    +{facts} fact{facts === 1 ? "" : "s"}
                  </span>
                )}
                {version != null && (
                  <span className="text-[9px] font-mono bg-muted/40 px-1 rounded text-muted-foreground/70">
                    v{String(version)}
                  </span>
                )}
              </li>
            );
          }
          if (evt.event_type === "cost_summary") {
            const callsTotal = Number(payload.calls_total ?? 0);
            const callsSkipped = Number(payload.calls_skipped ?? 0);
            return (
              <li
                key={`${evt.ts}-${idx}`}
                className="flex items-center gap-1.5 text-[11px] border-l-2 pl-2 border-amber-400"
              >
                <span className="font-mono text-[10px] text-muted-foreground tabular-nums w-9">
                  {elapsed}
                </span>
                <Sparkles size={11} className="text-amber-500" />
                <span className="text-foreground/85">
                  Wiki build · {callsTotal} call{callsTotal === 1 ? "" : "s"}
                  {callsSkipped > 0 && (
                    <span className="text-muted-foreground/70">
                      {" "}
                      ({callsSkipped} cached)
                    </span>
                  )}
                </span>
              </li>
            );
          }
          // parse_failure
          const pageId = String(payload.page_id ?? "");
          return (
            <li
              key={`${evt.ts}-${idx}`}
              className="flex items-center gap-1.5 text-[11px] border-l-2 pl-2 border-red-400"
            >
              <span className="font-mono text-[10px] text-muted-foreground tabular-nums w-9">
                {elapsed}
              </span>
              <AlertCircle size={11} className="text-red-500" />
              <span className="text-foreground/85">
                Parse failure · {pageId}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// SyncProgressV2 — top-level container
// ─────────────────────────────────────────────────────────────────────────

export function SyncProgressV2({
  channelId,
  phases,
  state,
  events,
  stageDetails,
  batchResults,
  batchResultsJobId,
  currentJobId,
  smoothedEtaSeconds,
  parseFailureState,
  totalMessages,
  processedMessages,
  totalBatches,
  batchesCompleted,
  startedAt,
  collapsed: collapsedProp,
  onCollapsedChange,
}: SyncProgressV2Props) {
  const [activeTab, setActiveTab] = useLocalStorageState<TabId>(
    "beever.monitor.activeTab",
    "activity",
  );
  // Internal collapse state (uncontrolled mode) — used only when the
  // caller doesn't pass ``collapsed``/``onCollapsedChange``.
  const [internalCollapsed, setInternalCollapsed] = useLocalStorageState<boolean>(
    "beever.monitor.collapsed",
    false,
  );
  const collapsed = collapsedProp ?? internalCollapsed;
  const setCollapsed = (next: boolean | ((prev: boolean) => boolean)) => {
    const resolved =
      typeof next === "function"
        ? (next as (p: boolean) => boolean)(collapsed)
        : next;
    if (onCollapsedChange) onCollapsedChange(resolved);
    else setInternalCollapsed(resolved);
  };
  // (Resizable activity panel was dropped — fullscreen layout supersedes
  //  the drag-to-resize feature. Panel naturally fills the card via
  //  flex-1 + min-h-0 on the body.)
  const activePhase = useMemo(
    () => deriveActivePhase(state, phases),
    [state, phases],
  );

  // Throughput: count message_processing events in the last 60 seconds.
  const throughput = useMemo(() => {
    const cutoff = Date.now() - 60_000;
    return events.filter((e) => {
      if (e.event_type !== "message_processing") return false;
      try {
        return new Date(e.ts).getTime() >= cutoff;
      } catch {
        return false;
      }
    }).length;
  }, [events]);

  const showParseBanner = parseFailureState?.should_show_banner ?? false;

  // Worker-flow fallback + sticky accumulator: ``batch_results`` is
  // populated only when ``BatchProcessor.update_sync_progress`` writes
  // to the user-facing sync_jobs row. The decoupled ExtractionWorker
  // writes to synthetic ``worker:<channel>:<ts>`` rows, so ``batchResults``
  // is empty for the entire run. Compose from ``activity_log`` instead.
  //
  // Server-side ``activity_log`` is $sliced to the last 50 entries, so
  // earlier batches' data scrolls off as the sync progresses. We keep a
  // per-sync client-side accumulator (``stickyResultsRef``) so once a
  // batch's facts/entities/samples land, they persist for the rest of
  // the sync — the Batch Results tab grows monotonically instead of
  // flickering as entries evict.
  const stickyResultsRef = useRef<Map<number, BatchResultEntry>>(new Map());
  const lastStartedAtRef = useRef<string | null>(null);
  const lastJobIdRef = useRef<string | null>(null);
  // Reset the sticky batch-results accumulator when a new sync starts —
  // either a different ``started_at`` OR a different ``currentJobId``.
  // The job_id check closes the race window where the parent has already
  // swapped to a new job (via triggerSync optimistic update) but the
  // first /sync/status poll for the new job hasn't returned yet — the
  // previous ``started_at`` would otherwise be sticky for one extra
  // tick, leaking the prior run's batches into the new view.
  if (
    lastStartedAtRef.current !== (startedAt ?? null) ||
    lastJobIdRef.current !== (currentJobId ?? null)
  ) {
    stickyResultsRef.current = new Map();
    lastStartedAtRef.current = startedAt ?? null;
    lastJobIdRef.current = currentJobId ?? null;
  }
  // Compute batches summary at the container level so both the activity
  // tab AND the batch-results tab share the same state source.
  const activityLog = stageDetails?.activity_log ?? [];
  // Confirmed-done set: union of (a) persister stage_output entries
  // in activity_log and (b) batch_nums already accumulated in the
  // sticky results buffer. Either source proves the batch finished
  // — and using BOTH closes the window between persister-done and
  // batch_results-row-arrival. Computed at the container level so
  // it can be passed DOWN to BatchFilteredActivityLog (whose own
  // activity_log slice has lost early batches' persister events
  // due to server-side $slice eviction).
  const knownDoneBatchNums = useMemo(() => {
    const s = new Set<number>();
    // Source 1: persister events still in the activity_log slice.
    for (const e of activityLog) {
      if (
        e.type === "stage_output" &&
        e.agent === "persister" &&
        typeof e.batch_idx === "number"
      ) {
        s.add(e.batch_idx);
      }
    }
    // Source 2: sticky results accumulator (populated by
    // ``derivedBatchResults`` when batch_results is server-empty).
    for (const bn of stickyResultsRef.current.keys()) {
      s.add(bn);
    }
    // Source 3: SERVER-PROVIDED ``batch_results`` directly — but ONLY
    // when the response's ``job_id`` matches the caller's current
    // ``currentJobId``. Otherwise we risk absorbing the previous sync's
    // done chips during the brief window after the user clicks "Sync
    // Channel" but before the new ``sync_jobs`` row lands. The backend
    // race window leaks the prior run's ``batch_results`` array to
    // ``/sync/status`` for a few hundred ms; without this gate the
    // chip strip jumps to DONE before any work has happened.
    //
    // When ``currentJobId`` is undefined we preserve the previous
    // unconditional behaviour for back-compat with tests + callers that
    // haven't threaded job_id through yet.
    const jobIdsAligned =
      currentJobId === undefined ||
      currentJobId === null ||
      batchResultsJobId === undefined ||
      batchResultsJobId === null ||
      batchResultsJobId === currentJobId;
    if (jobIdsAligned) {
      for (const r of batchResults ?? []) {
        if (typeof r.batch_num === "number") {
          s.add(r.batch_num);
        }
      }
    }
    return s;
  }, [activityLog, batchResults, batchResultsJobId, currentJobId]);
  const batchSummaries = useMemo(
    () => summariseBatches(activityLog, totalBatches, batchesCompleted, knownDoneBatchNums),
    [activityLog, totalBatches, batchesCompleted, knownDoneBatchNums],
  );

  const derivedBatchResults = useMemo<BatchResultEntry[]>(() => {
    // Server-provided ``batch_results`` (legacy in-process flow) wins
    // when populated — that path already accumulates server-side.
    // BUT: gate on job_id alignment so we don't render the previous
    // sync's batch_results during the brief trigger→new-row window.
    // When job ids are not provided by the caller, fall back to the
    // legacy unconditional behaviour for back-compat.
    const jobIdsAlignedHere =
      currentJobId === undefined ||
      currentJobId === null ||
      batchResultsJobId === undefined ||
      batchResultsJobId === null ||
      batchResultsJobId === currentJobId;
    if (jobIdsAlignedHere && batchResults && batchResults.length > 0) {
      return batchResults;
    }
    const fresh = deriveBatchResultsFromActivity(activityLog);
    for (const r of fresh) {
      const prev = stickyResultsRef.current.get(r.batch_num);
      if (!prev) {
        stickyResultsRef.current.set(r.batch_num, r);
        continue;
      }
      // Merge — keep the highest seen counts and union the sample lists
      // so partial activity_log frames don't shrink an already-rich
      // batch entry.
      stickyResultsRef.current.set(r.batch_num, {
        batch_num: r.batch_num,
        facts_count: Math.max(prev.facts_count, r.facts_count),
        entities_count: Math.max(prev.entities_count, r.entities_count),
        relationships_count: Math.max(
          prev.relationships_count,
          r.relationships_count,
        ),
        embedded_count: Math.max(
          prev.embedded_count ?? 0,
          r.embedded_count ?? 0,
        ),
        media_count: Math.max(prev.media_count ?? 0, r.media_count ?? 0),
        sample_facts: r.sample_facts.length > 0
          ? r.sample_facts
          : prev.sample_facts,
        sample_entities: r.sample_entities.length > 0
          ? r.sample_entities
          : prev.sample_entities,
        sample_relationships: r.sample_relationships.length > 0
          ? r.sample_relationships
          : prev.sample_relationships,
        duration_seconds: Math.max(prev.duration_seconds, r.duration_seconds),
        error: r.error ?? prev.error,
      });
    }
    // Build the final list: one entry per batch in batchSummaries (1..N),
    // merging in sticky data when present. This ensures pending /
    // running batches without samples yet still appear in the list with
    // accurate state — the user sees ALL batches and can filter by state.
    const merged: BatchResultEntry[] = batchSummaries.map((b) => {
      const sticky = stickyResultsRef.current.get(b.batchIdx);
      return {
        batch_num: b.batchIdx,
        facts_count: sticky?.facts_count ?? b.factsCount,
        entities_count: sticky?.entities_count ?? b.entitiesCount,
        relationships_count: sticky?.relationships_count ?? 0,
        embedded_count: sticky?.embedded_count ?? 0,
        media_count: sticky?.media_count ?? 0,
        state: b.state,
        sample_facts: sticky?.sample_facts ?? [],
        sample_entities: sticky?.sample_entities ?? [],
        sample_relationships: sticky?.sample_relationships ?? [],
        duration_seconds: sticky?.duration_seconds ?? (b.totalElapsedMs > 0 ? b.totalElapsedMs / 1000 : 0),
        error: sticky?.error ?? null,
      };
    });
    return merged;
  }, [batchResults, batchResultsJobId, currentJobId, activityLog, batchSummaries]);

  const batchCount = derivedBatchResults.length;

  // Cumulative elapsed time from started_at.
  const elapsedHeader = useMemo(
    () => (startedAt ? fmtElapsed(startedAt, Date.now()) : null),
    [startedAt],
  );

  return (
    <div
      className="rounded-lg border border-border bg-card overflow-hidden relative flex flex-col h-full min-h-0"
      data-testid={`sync-progress-v2-${channelId}`}
    >
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        title={collapsed ? "Expand pipeline monitor" : "Collapse pipeline monitor"}
        aria-label={collapsed ? "Expand pipeline monitor" : "Collapse pipeline monitor"}
        className="absolute top-2 right-2 z-10 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border bg-card text-muted-foreground hover:text-foreground hover:bg-muted/60 hover:border-primary/40 transition-colors shadow-sm"
      >
        <span className="text-[10px] font-medium uppercase tracking-wider">
          {collapsed ? "Expand" : "Collapse"}
        </span>
        {collapsed ? (
          <ChevronDown size={14} />
        ) : (
          <ChevronUp size={14} />
        )}
      </button>
      {/* Indeterminate "starting" bar — shown only during the brief window
       *  between the sync trigger and the first /sync/status poll
       *  returning real phase data. Replaces the otherwise-blank
       *  monitor appearance with a clear "we're waiting on the server"
       *  signal. */}
      {phases.length === 0 && (state === "syncing") && (
        <div className="h-0.5 w-full bg-muted overflow-hidden" aria-hidden>
          <div className="h-full w-1/3 bg-gradient-to-r from-transparent via-primary to-transparent motion-safe:animate-stepper-shimmer" />
        </div>
      )}
      <PipelineStepper phases={phases} activePhase={activePhase} />
      <ProgressHeader
        activePhase={activePhase}
        totalMessages={totalMessages}
        processedMessages={processedMessages}
        smoothedEtaSeconds={smoothedEtaSeconds}
        startedAt={startedAt}
        phases={phases}
      />
      {/* Collapsible body — pipeline detail. The stepper + header stay
       *  visible at all times so the user always sees the high-level
       *  phase. Click the chevron in the top-right to fold the detail. */}
      <div
        className={cn(
          "transition-all duration-300 ease-out overflow-hidden",
          collapsed
            ? "max-h-0 opacity-0"
            : "flex-1 min-h-0 flex flex-col opacity-100",
        )}
        aria-hidden={collapsed}
      >
      <MetricsBar
        events={events}
        activityLog={stageDetails?.activity_log ?? []}
        stickyResults={derivedBatchResults}
        totalMessages={totalMessages}
        processedMessages={processedMessages}
        totalBatches={totalBatches}
        batchesCompleted={batchesCompleted}
      />
      {showParseBanner && parseFailureState && (
        <div
          role="alert"
          className="flex items-start gap-2 border-b border-amber-200 dark:border-amber-900 bg-amber-50/60 dark:bg-amber-950/30 px-3 py-2"
        >
          <AlertCircle
            size={14}
            className="shrink-0 mt-0.5 text-amber-600 dark:text-amber-400"
          />
          <div className="text-xs text-amber-800 dark:text-amber-200">
            {parseFailureState.count_last_10_min} wiki update
            {parseFailureState.count_last_10_min === 1 ? "" : "s"} failed in
            the last 10 minutes.
          </div>
        </div>
      )}
      <Tabs
        active={activeTab}
        onChange={setActiveTab}
        batchCount={batchCount}
        channelId={channelId}
      />
      {/* Activity / Batch Results body — fills the remaining card height
       *  when the monitor is rendered in fullscreen mode (wiki tab).
       *  In compact mode (other tabs) the parent constrains height
       *  externally, so the panel just scrolls within whatever space
       *  it's given. */}
      <div className="px-3 py-2 bg-card overflow-y-auto flex-1 min-h-0">
        {activeTab === "activity" ? (
          <BatchFilteredActivityLog
            stageDetails={stageDetails}
            totalBatches={totalBatches}
            batchesCompleted={batchesCompleted}
            channelId={channelId}
            startedAt={startedAt}
            knownDoneBatchNums={knownDoneBatchNums}
          />
        ) : (
          <BatchResults results={derivedBatchResults} />
        )}
      </div>
      <LiveEventsList events={events} startedAt={startedAt} />
      <div className="flex items-center flex-wrap gap-3 border-t border-border bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground">
        <span>
          Throughput:{" "}
          <span className="font-medium text-foreground">{throughput}</span>{" "}
          msg/min
        </span>
        {elapsedHeader && (
          <span>
            Elapsed:{" "}
            <span className="font-medium text-foreground">{elapsedHeader}</span>
          </span>
        )}
        <CostSummaryBadge events={events} />
        <span className="ml-auto">
          Parse failures (10m):{" "}
          <span
            className={cn(
              "font-medium",
              showParseBanner
                ? "text-amber-600 dark:text-amber-400"
                : "text-foreground",
            )}
          >
            {parseFailureState?.count_last_10_min ?? 0}
          </span>
        </span>
      </div>
      </div>
    </div>
  );
}

export default SyncProgressV2;
