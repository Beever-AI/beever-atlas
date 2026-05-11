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
  ExternalLink,
  FileText,
  Loader2,
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
  smoothedEtaSeconds?: number | null;
  parseFailureState?: ParseFailureState | null;
  totalMessages?: number;
  processedMessages?: number;
  startedAt?: string | null;
  retrying?: number;
  abandoned?: number;
}

const PHASE_DISPLAY_ORDER: Array<{ name: PhaseName; label: string }> = [
  { name: "fetched", label: "Fetch" },
  { name: "extracting", label: "Extract" },
  { name: "wiki_maintenance", label: "Wiki" },
  { name: "overview_wiki", label: "Done" },
];

function deriveActivePhase(
  state: "idle" | "syncing" | "error",
  phases: Phase[],
): ActivePhase {
  if (state === "error") return "error";
  const byName = (n: PhaseName) => phases.find((p) => p.name === n);
  if (phases.some((p) => p.state === "failed")) return "error";
  if (byName("fetched")?.state === "in_flight" || state === "syncing") {
    return "syncing";
  }
  if (byName("extracting")?.state === "in_flight") return "extracting";
  if (
    byName("wiki_maintenance")?.state === "in_flight" ||
    byName("overview_wiki")?.state === "in_flight"
  ) {
    return "building";
  }
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
      <div className="flex flex-col items-center gap-1 shrink-0">
        <div
          className={cn(
            "w-2.5 h-2.5 rounded-full transition-colors duration-300 shrink-0",
            state === "active" &&
              "bg-primary ring-2 ring-primary/30 animate-pulse",
            state === "done" && "bg-emerald-500",
            state === "failed" && "bg-red-500",
            state === "pending" && "bg-muted-foreground/30",
          )}
        />
        <span
          className={cn(
            "text-[10px] uppercase tracking-wide whitespace-nowrap",
            state === "active" && "text-primary font-medium",
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
            "flex-1 h-px transition-colors duration-300 min-w-[20px]",
            state === "done"
              ? "bg-emerald-500/40"
              : "bg-muted-foreground/20",
          )}
        />
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

interface PipelineMetrics {
  totalBatches: number;
  batchesDone: number;
  batchesInFlight: number;
  totalFacts: number;
  totalEntities: number;
  totalRelationships: number;
  totalEmbedded: number;
  totalMediaEnriched: number;
}

function deriveMetrics(
  events: RecentEvent[],
  activityLog: ActivityEntry[],
  phases: Phase[],
): PipelineMetrics {
  // Total batches: prefer ``phases.extracting.total`` (authoritative);
  // otherwise infer from the max ``batch_idx`` seen in activity_log.
  const extractPhase = phases.find((p) => p.name === "extracting");
  const totalFromPhase = extractPhase?.total ?? 0;
  const maxBatchIdx = activityLog.reduce(
    (m, e) => Math.max(m, (e.batch_idx ?? 0)),
    0,
  );
  const totalBatches = Math.max(
    totalFromPhase,
    maxBatchIdx > 0 ? maxBatchIdx : 0,
  );

  // Per-batch state: a batch is "done" when a persister stage_output
  // entry exists for it; "in_flight" when there's any stage_start but
  // no persister output yet.
  const batchHasPersisterDone = new Map<number, boolean>();
  const batchHasStart = new Set<number>();
  for (const e of activityLog) {
    const idx = e.batch_idx;
    if (idx == null) continue;
    if (e.type === "stage_start") {
      batchHasStart.add(idx);
    } else if (
      e.type === "stage_output" &&
      e.agent === "persister"
    ) {
      batchHasPersisterDone.set(idx, true);
    }
  }
  const batchesDone = Array.from(batchHasPersisterDone.values()).filter(
    Boolean,
  ).length;
  const batchesInFlight = Math.max(
    0,
    batchHasStart.size - batchesDone,
  );

  // Aggregate fact / entity / embedding counts from stage_output metrics.
  let totalFacts = 0;
  let totalEntities = 0;
  let totalRelationships = 0;
  let totalEmbedded = 0;
  let totalMediaEnriched = 0;
  for (const e of activityLog) {
    if (e.type !== "stage_output") continue;
    const m = e.metrics ?? {};
    if (e.agent === "fact_extractor") {
      totalFacts += Number(m.count ?? 0);
    } else if (e.agent === "entity_extractor") {
      totalEntities += Number(m.entities ?? 0);
      totalRelationships += Number(m.relationships ?? 0);
    } else if (e.agent === "embedder") {
      totalEmbedded += Number(m.embedded ?? 0);
    } else if (e.agent === "preprocessor") {
      totalMediaEnriched += Number(m.media_enriched ?? 0);
    }
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
  return (
    <div className="flex flex-col gap-0.5 min-w-0">
      <div className="text-[9px] uppercase tracking-wider text-muted-foreground/60 font-medium">
        {label}
      </div>
      <div className="flex items-baseline gap-1">
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

function MetricsBar({
  events,
  activityLog,
  phases,
  totalMessages,
  processedMessages,
}: {
  events: RecentEvent[];
  activityLog: ActivityEntry[];
  phases: Phase[];
  totalMessages?: number;
  processedMessages?: number;
}) {
  const m = useMemo(
    () => deriveMetrics(events, activityLog, phases),
    [events, activityLog, phases],
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

interface BatchSummary {
  batchIdx: number;
  state: "pending" | "running" | "done" | "failed";
  stagesStarted: number;
  hasPersisterDone: boolean;
  factsCount: number;
  entitiesCount: number;
  totalElapsedMs: number;
  hasFailure: boolean;
}

function summariseBatches(
  activityLog: ActivityEntry[],
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
  // Derive state per batch.
  for (const s of byBatch.values()) {
    if (s.hasFailure) s.state = "failed";
    else if (s.hasPersisterDone) s.state = "done";
    else if (s.stagesStarted > 0) s.state = "running";
    else s.state = "pending";
  }
  return Array.from(byBatch.values()).sort((a, b) => a.batchIdx - b.batchIdx);
}

function BatchTabs({
  batches,
  selected,
  onSelect,
}: {
  batches: BatchSummary[];
  selected: number | "all";
  onSelect: (sel: number | "all") => void;
}) {
  if (batches.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1 px-3 py-1.5 border-b border-border bg-muted/5">
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
        All ({batches.length})
      </button>
      <span className="text-muted-foreground/30">·</span>
      {batches.map((b) => {
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
              "inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-mono rounded transition-colors",
              isSelected
                ? "text-primary bg-primary/10 border border-primary/20"
                : "text-muted-foreground hover:text-foreground hover:bg-muted/40",
            )}
          >
            <span className={stateColor}>{stateIcon}</span>
            Batch {b.batchIdx}
          </button>
        );
      })}
    </div>
  );
}

function BatchFilteredActivityLog({
  stageDetails,
}: {
  stageDetails?: {
    activity_log?: ActivityEntry[];
    [k: string]: unknown;
  };
}) {
  const [selectedBatch, setSelectedBatch] = useState<number | "all">("all");
  const activityLog = stageDetails?.activity_log ?? [];

  const batches = useMemo(() => summariseBatches(activityLog), [activityLog]);

  // Auto-follow: snap to the latest running batch when the user hasn't
  // manually picked one yet. Once they click a tab, we honour it.
  const hasUserSelection = useRef(false);
  useEffect(() => {
    if (hasUserSelection.current) return;
    const running = batches.filter((b) => b.state === "running");
    if (running.length > 0) {
      setSelectedBatch(running[running.length - 1].batchIdx);
    }
  }, [batches]);

  const filteredDetails = useMemo(() => {
    if (selectedBatch === "all") return stageDetails;
    return {
      ...stageDetails,
      activity_log: activityLog.filter((e) => e.batch_idx === selectedBatch),
    };
  }, [stageDetails, activityLog, selectedBatch]);

  return (
    <div>
      <BatchTabs
        batches={batches}
        selected={selectedBatch}
        onSelect={(sel) => {
          hasUserSelection.current = true;
          setSelectedBatch(sel);
        }}
      />
      <ActivityLog details={filteredDetails} />
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
  smoothedEtaSeconds,
  parseFailureState,
  totalMessages,
  processedMessages,
  startedAt,
}: SyncProgressV2Props) {
  const [activeTab, setActiveTab] = useState<TabId>("activity");
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
  const batchCount = batchResults?.length ?? 0;

  // Cumulative elapsed time from started_at.
  const elapsedHeader = useMemo(
    () => (startedAt ? fmtElapsed(startedAt, Date.now()) : null),
    [startedAt],
  );

  return (
    <div
      className="rounded-lg border border-border bg-card overflow-hidden"
      data-testid={`sync-progress-v2-${channelId}`}
    >
      <PipelineStepper phases={phases} activePhase={activePhase} />
      <ProgressHeader
        activePhase={activePhase}
        totalMessages={totalMessages}
        processedMessages={processedMessages}
        smoothedEtaSeconds={smoothedEtaSeconds}
        startedAt={startedAt}
        phases={phases}
      />
      <MetricsBar
        events={events}
        activityLog={stageDetails?.activity_log ?? []}
        phases={phases}
        totalMessages={totalMessages}
        processedMessages={processedMessages}
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
      <div className="px-3 py-2 bg-card max-h-[480px] overflow-y-auto">
        {activeTab === "activity" ? (
          <BatchFilteredActivityLog stageDetails={stageDetails} />
        ) : (
          <BatchResults results={batchResults ?? []} />
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
  );
}

export default SyncProgressV2;
