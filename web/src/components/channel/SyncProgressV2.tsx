/**
 * SyncProgressV2 — single-card pipeline-aware progress monitor.
 *
 * Replaces the three-pane SyncMonitor + legacy PhasedProgressCard combo
 * with one vertical card:
 *
 *   ┌───────────────────────────────────────────────────────────┐
 *   │ (●) Fetch ──── (●) Extract ──── (○) Wiki ──── (○) Done    │  PipelineStepper
 *   ├───────────────────────────────────────────────────────────┤
 *   │ (spinner) Extracting facts   142 / 711  20%  ETA ~3 min   │  ProgressHeader
 *   │ [============>                                          ] │
 *   ├───────────────────────────────────────────────────────────┤
 *   │ ACTIVITY                                                  │  ActivityStream
 *   │ 0:42  fact_extractor running  batch 7   gemini-flash      │
 *   │ 0:41  entity_extractor done  batch 6  1.2s                │
 *   │ ...                                                       │
 *   ├───────────────────────────────────────────────────────────┤
 *   │ Throughput: 12 msg/min  |  Parse failures (10m): 0        │
 *   └───────────────────────────────────────────────────────────┘
 *
 * Phase logic is the waterfall in design D1 — never trusts ``state``
 * alone, so the header reads "Extracting facts" while ``state===idle``
 * but ``phases.extracting===in_flight``. This kills the
 * "Sync complete at 28%" bug.
 */

import { useMemo } from "react";
import {
  AlertCircle,
  AlertTriangle,
  Activity,
  CheckCircle2,
  FileText,
  Loader2,
  MessageSquare,
  Sparkles,
  XCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type {
  ParseFailureState,
  Phase,
  PhaseName,
  RecentEvent,
} from "@/lib/types";

// ─────────────────────────────────────────────────────────────────────────
// Phase model
// ─────────────────────────────────────────────────────────────────────────

type ActivePhase = "syncing" | "extracting" | "building" | "done" | "error";

interface SyncProgressV2Props {
  channelId: string;
  phases: Phase[];
  state: "idle" | "syncing" | "error";
  events: RecentEvent[];
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
  // Any failed phase => error
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
// Utility — elapsed-time formatter and event helpers
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

function fmtDurationMs(ms: number | undefined): string {
  if (ms == null) return "";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
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

const AGENT_LABELS: Record<string, string> = {
  fact_extractor: "fact_extractor",
  entity_extractor: "entity_extractor",
  coreference_resolver: "coref_resolver",
  embedder: "embedder",
  persister: "persister",
  wiki_maintainer: "wiki_maintainer",
};

// ─────────────────────────────────────────────────────────────────────────
// PipelineStepper — 4 dots + connectors
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
            state === "active" && "bg-primary ring-2 ring-primary/30 animate-pulse",
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
            state === "done" ? "bg-emerald-500/40" : "bg-muted-foreground/20",
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
  const dotState = (name: PhaseName): "pending" | "active" | "done" | "failed" => {
    const p = phases.find((ph) => ph.name === name);
    if (p?.state === "done" || p?.state === "skipped") return "done";
    if (p?.state === "failed") return "failed";
    // Map active phase to stepper position.
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
// ProgressHeader — active-phase label + count + ETA + progress bar
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
  // Choose icon by phase.
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

  // Progress numerator/denominator depends on phase. During building,
  // use wiki_maintenance phase's done/total when available — that's the
  // bottleneck operators want to track.
  const wikiPhase = phases.find((p) => p.name === "wiki_maintenance");
  const useWikiNumbers =
    activePhase === "building" && (wikiPhase?.total ?? 0) > 0;
  const done = useWikiNumbers ? wikiPhase?.done ?? 0 : processedMessages ?? 0;
  const total = useWikiNumbers ? wikiPhase?.total ?? 0 : totalMessages ?? 0;
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
// ActivityStream — single chronological feed with per-event-type rows
// ─────────────────────────────────────────────────────────────────────────

interface ActivityRowProps {
  event: RecentEvent;
  startedAt?: string | null;
}

function ActivityRow({ event, startedAt }: ActivityRowProps) {
  const elapsed = useMemo(() => {
    try {
      const evtMs = new Date(event.ts).getTime();
      return fmtElapsed(startedAt, evtMs);
    } catch {
      return "—";
    }
  }, [event.ts, startedAt]);

  const type = event.event_type ?? "legacy";
  const payload = event.payload ?? {};

  if (type === "agent_state") {
    const agent = String(payload.agent ?? "");
    const state = String(payload.state ?? "");
    const batchId = String(payload.batch_id ?? "");
    const elapsedMs =
      typeof payload.elapsed_ms === "number"
        ? (payload.elapsed_ms as number)
        : undefined;

    const isRunning = state === "running";
    const isFailed = state === "failed";

    const borderColor = isFailed
      ? "border-red-400"
      : isRunning
        ? "border-primary"
        : "border-emerald-400";
    const StateIcon = isFailed ? XCircle : isRunning ? Activity : CheckCircle2;
    const stateIconClass = isFailed
      ? "text-red-500"
      : isRunning
        ? "text-primary animate-pulse"
        : "text-emerald-500";

    return (
      <li
        className={cn(
          "border-l-2 pl-2 py-1 text-xs",
          borderColor,
          "animate-in fade-in slide-in-from-top-1 duration-200",
        )}
      >
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-[10px] text-muted-foreground tabular-nums w-9 shrink-0">
            {elapsed}
          </span>
          <StateIcon size={11} className={cn("shrink-0", stateIconClass)} />
          <span className="font-medium text-foreground/85">
            {AGENT_LABELS[agent] ?? agent}
          </span>
          <span className="text-muted-foreground/70">{state}</span>
          {batchId && (
            <span className="text-[10px] font-mono text-muted-foreground/70 bg-muted/40 px-1 rounded">
              batch {batchId.split(":").slice(-1)[0]}
            </span>
          )}
          {elapsedMs != null && (
            <span className="ml-auto text-[10px] font-mono text-muted-foreground">
              {fmtDurationMs(elapsedMs)}
            </span>
          )}
        </div>
      </li>
    );
  }

  if (type === "wiki_update") {
    const pageTitle = String(payload.page_title ?? payload.page_id ?? event.label);
    const facts = Number(payload.facts_integrated ?? 0);
    const version = payload.version;
    const action = String(payload.action ?? "patched");
    const isSkipped = action === "skipped_frozen";
    return (
      <li
        className={cn(
          "border-l-2 pl-2 py-1 text-xs",
          isSkipped ? "border-amber-400" : "border-violet-400",
          "animate-in fade-in slide-in-from-top-1 duration-200",
        )}
      >
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-[10px] text-muted-foreground tabular-nums w-9 shrink-0">
            {elapsed}
          </span>
          <FileText
            size={11}
            className={cn(
              "shrink-0",
              isSkipped ? "text-amber-500" : "text-violet-500",
            )}
          />
          <span className="font-medium text-foreground/85 truncate">
            {isSkipped ? "Skipped (frozen)" : "Updated"} "{pageTitle}"
          </span>
          {facts > 0 && (
            <span className="text-muted-foreground/70">
              +{facts} fact{facts === 1 ? "" : "s"}
            </span>
          )}
          {version != null && (
            <span className="text-[10px] font-mono text-muted-foreground/70 bg-muted/40 px-1 rounded">
              v{String(version)}
            </span>
          )}
        </div>
      </li>
    );
  }

  if (type === "cost_summary") {
    const callsTotal = Number(payload.calls_total ?? 0);
    const callsSkipped = Number(payload.calls_skipped ?? 0);
    const durationMs =
      typeof payload.duration_ms === "number"
        ? (payload.duration_ms as number)
        : 0;
    return (
      <li
        className={cn(
          "border-l-2 pl-2 py-1 text-xs border-amber-400",
          "animate-in fade-in slide-in-from-top-1 duration-200",
        )}
      >
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-[10px] text-muted-foreground tabular-nums w-9 shrink-0">
            {elapsed}
          </span>
          <Sparkles size={11} className="shrink-0 text-amber-500" />
          <span className="font-medium text-foreground/85">
            Wiki build · {callsTotal} LLM call{callsTotal === 1 ? "" : "s"}
            {callsSkipped > 0 && (
              <span className="text-muted-foreground/70">
                {" "}({callsSkipped} cached)
              </span>
            )}
          </span>
          {durationMs > 0 && (
            <span className="ml-auto text-[10px] font-mono text-muted-foreground">
              {fmtDurationMs(durationMs)}
            </span>
          )}
        </div>
      </li>
    );
  }

  if (type === "parse_failure") {
    const pageId = String(payload.page_id ?? "");
    const rawLen = Number(payload.raw_len ?? 0);
    return (
      <li
        className={cn(
          "border-l-2 pl-2 py-1 text-xs border-red-400",
          "animate-in fade-in slide-in-from-top-1 duration-200",
        )}
      >
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-[10px] text-muted-foreground tabular-nums w-9 shrink-0">
            {elapsed}
          </span>
          <AlertCircle size={11} className="shrink-0 text-red-500" />
          <span className="font-medium text-foreground/85">
            Parse failure · {pageId}
          </span>
          {rawLen > 0 && (
            <span className="text-muted-foreground/70">{rawLen} chars</span>
          )}
        </div>
      </li>
    );
  }

  if (type === "message_processing") {
    const preview = String(payload.text_preview ?? event.label).slice(0, 80);
    const author = String(payload.author ?? "");
    return (
      <li
        className={cn(
          "border-l-2 pl-2 py-1 text-xs border-sky-400/70",
          "animate-in fade-in slide-in-from-top-1 duration-200",
        )}
      >
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-[10px] text-muted-foreground tabular-nums w-9 shrink-0">
            {elapsed}
          </span>
          <MessageSquare size={11} className="shrink-0 text-sky-500" />
          <span className="text-foreground/80 truncate">
            {author && (
              <span className="text-muted-foreground">@{author}: </span>
            )}
            {preview}
          </span>
        </div>
      </li>
    );
  }

  // Legacy fallback — plain label.
  return (
    <li className="border-l-2 pl-2 py-1 text-xs border-muted-foreground/30 animate-in fade-in slide-in-from-top-1 duration-200">
      <div className="flex items-center gap-1.5">
        <span className="font-mono text-[10px] text-muted-foreground tabular-nums w-9 shrink-0">
          {elapsed}
        </span>
        <span className="text-[10px] text-muted-foreground uppercase">
          {event.stage}
        </span>
        <span className="text-foreground/75 truncate">{event.label}</span>
      </div>
    </li>
  );
}

function ActivityStream({
  events,
  startedAt,
  maxRows = 12,
}: {
  events: RecentEvent[];
  startedAt?: string | null;
  maxRows?: number;
}) {
  // Newest first — backend already returns newest-first per ``recent_for``.
  const visible = useMemo(() => events.slice(0, maxRows), [events, maxRows]);

  return (
    <div className="bg-card px-3 py-2">
      <div className="flex items-center justify-between mb-1.5">
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground font-semibold">
          Activity
        </div>
        {events.length > maxRows && (
          <div className="text-[10px] text-muted-foreground/70">
            Showing {maxRows} of {events.length}
          </div>
        )}
      </div>
      {visible.length === 0 ? (
        <div className="text-xs text-muted-foreground/60 italic py-3">
          Waiting for the first event…
        </div>
      ) : (
        <ul className="space-y-0.5 max-h-[280px] overflow-y-auto">
          {visible.map((evt, idx) => (
            <ActivityRow
              key={`${evt.ts}-${idx}`}
              event={evt}
              startedAt={startedAt}
            />
          ))}
        </ul>
      )}
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
  smoothedEtaSeconds,
  parseFailureState,
  totalMessages,
  processedMessages,
  startedAt,
}: SyncProgressV2Props) {
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
      <ActivityStream events={events} startedAt={startedAt} />
      <div className="flex items-center justify-between gap-3 border-t border-border bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground">
        <span>
          Throughput:{" "}
          <span className="font-medium text-foreground">{throughput}</span>{" "}
          msg/min
        </span>
        <span>
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
