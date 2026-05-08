/**
 * PhasedProgressCard
 *
 * Renders the four-phase pipeline (fetched → extracting → wiki_maintenance
 * → overview_wiki) surfaced by ``GET /api/channels/{id}/sync/status``
 * (PR-3 — sync-pipeline-feedback-and-auto-wiki). Replaces the legacy
 * decoupled-mode rendering in ``SyncProgress.tsx`` whenever the new
 * ``phases`` field is present in the payload.
 *
 * Layout:
 *   1. Header — active phase + smoothed ETA + retrying/abandoned chips
 *   2. Four phase rows (one per ``PhaseName``)
 *      - state icon (✓ done, ⟳ in_flight, ○ pending, ⏭ skipped, ✗ failed)
 *      - phase label + last_event_label / progress fraction
 *      - per-row progress bar (when ``done`` + ``total`` are populated)
 */
import {
  CheckCircle2,
  Circle,
  Loader2,
  SkipForward,
  XCircle,
  Clock,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { Phase, PhaseName, PhaseState } from "@/lib/types";

const PHASE_ORDER: PhaseName[] = [
  "fetched",
  "extracting",
  "wiki_maintenance",
  "overview_wiki",
];

const PHASE_LABELS: Record<PhaseName, string> = {
  fetched: "Fetched messages",
  extracting: "Extracting facts",
  wiki_maintenance: "Updating wiki pages",
  overview_wiki: "Generating overview wiki",
};

function StateIcon({ state }: { state: PhaseState }) {
  switch (state) {
    case "done":
      return (
        <CheckCircle2
          className="h-4 w-4 text-emerald-500 shrink-0"
          aria-label="done"
        />
      );
    case "in_flight":
      return (
        <Loader2
          className="h-4 w-4 text-primary animate-spin shrink-0"
          aria-label="in flight"
        />
      );
    case "skipped":
      return (
        <SkipForward
          className="h-4 w-4 text-muted-foreground shrink-0"
          aria-label="skipped"
        />
      );
    case "failed":
      return (
        <XCircle
          className="h-4 w-4 text-red-500 shrink-0"
          aria-label="failed"
        />
      );
    case "pending":
    default:
      return (
        <Circle
          className="h-4 w-4 text-muted-foreground shrink-0"
          aria-label="pending"
        />
      );
  }
}

function formatEta(seconds: number | null | undefined): string {
  if (seconds == null) return "Calculating…";
  if (seconds < 1) return "<1 s remaining";
  if (seconds < 60) return `${Math.round(seconds)} s remaining`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `~${minutes} min remaining`;
  return `~${Math.round(minutes / 60)} h remaining`;
}

function formatDuration(ms: number | null | undefined): string | null {
  if (!ms || ms <= 0) return null;
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${minutes.toFixed(1)} min`;
  return `${(minutes / 60).toFixed(1)} h`;
}

/** Per-row progress bar — only shown when the phase has numeric
 *  ``done``/``total``. Phases like ``overview_wiki`` are boolean shaped
 *  (it either exists or it doesn't) so we just show duration there. */
function PhaseProgressBar({ done, total, state }: { done: number; total: number; state: PhaseState }) {
  const pct = total > 0 ? Math.min(100, (done / total) * 100) : 0;
  const failed = state === "failed";
  return (
    <div
      className="h-1.5 w-full rounded-full bg-muted overflow-hidden"
      role="progressbar"
      aria-valuenow={done}
      aria-valuemin={0}
      aria-valuemax={total}
    >
      <div
        className={cn(
          "h-full rounded-full transition-all duration-700 ease-out",
          failed
            ? "bg-red-500"
            : state === "in_flight"
              ? "bg-primary"
              : "bg-emerald-500",
        )}
        style={{ width: `${Math.max(pct, state === "done" ? 100 : 0)}%` }}
      />
    </div>
  );
}

interface PhaseRowProps {
  phase: Phase;
  isActive: boolean;
  smoothedEtaSeconds: number | null | undefined;
  retrying?: number;
  abandoned?: number;
  onOpenFailedDrillDown?: (channelId: string) => void;
  channelId?: string;
}

function PhaseRow({
  phase,
  isActive,
  smoothedEtaSeconds,
  retrying,
  abandoned,
  onOpenFailedDrillDown,
  channelId,
}: PhaseRowProps) {
  const label = PHASE_LABELS[phase.name] ?? phase.name;
  const hasFraction = typeof phase.done === "number" && typeof phase.total === "number" && phase.total > 0;
  const fraction = hasFraction ? `${phase.done} / ${phase.total}` : null;
  const duration = formatDuration(phase.duration_ms);

  // The retrying / abandoned chips render inline beside the
  // ``extracting`` phase (where retries surface) per spec U3.
  const showRetryChips = phase.name === "extracting";

  return (
    <div
      data-testid={`phase-row-${phase.name}`}
      className={cn(
        "flex items-start gap-2.5 rounded-md px-2 py-1.5 transition-colors",
        isActive
          ? "bg-primary/5 border border-primary/15"
          : "border border-transparent",
      )}
    >
      <div className="mt-0.5">
        <StateIcon state={phase.state} />
      </div>

      <div className="min-w-0 flex-1 space-y-1">
        {/* Top row: label + fraction / duration / chips */}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span
            className={cn(
              "text-xs font-medium",
              phase.state === "done"
                ? "text-emerald-700 dark:text-emerald-400"
                : phase.state === "in_flight"
                  ? "text-foreground"
                  : phase.state === "failed"
                    ? "text-red-600 dark:text-red-400"
                    : phase.state === "skipped"
                      ? "text-muted-foreground line-through"
                      : "text-muted-foreground",
            )}
          >
            {label}
          </span>
          {fraction && (
            <span className="font-mono text-[10px] text-muted-foreground tabular-nums">
              {fraction}
            </span>
          )}
          {!hasFraction && duration && (
            <span className="font-mono text-[10px] text-muted-foreground">
              {duration}
            </span>
          )}
          {isActive && phase.state === "in_flight" && (
            <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
              <Clock className="h-3 w-3" aria-hidden />
              {formatEta(smoothedEtaSeconds)}
            </span>
          )}

          {showRetryChips && (retrying ?? 0) > 0 && (
            <span
              data-testid="retrying-chip"
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] bg-amber-500/10 text-amber-700 dark:text-amber-400 border border-amber-500/20"
            >
              Retrying — {retrying} {retrying === 1 ? "row" : "rows"}
            </span>
          )}
          {showRetryChips && (abandoned ?? 0) > 0 && (
            <button
              type="button"
              data-testid="abandoned-chip"
              onClick={() =>
                channelId && onOpenFailedDrillDown?.(channelId)
              }
              disabled={!onOpenFailedDrillDown || !channelId}
              className={cn(
                "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] border bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/20",
                onOpenFailedDrillDown && channelId
                  ? "hover:underline cursor-pointer"
                  : "cursor-default",
              )}
            >
              Abandoned — {abandoned} {abandoned === 1 ? "row" : "rows"}
            </button>
          )}
        </div>

        {/* Per-row progress bar — only when fraction known */}
        {hasFraction && (
          <PhaseProgressBar
            done={phase.done!}
            total={phase.total!}
            state={phase.state}
          />
        )}

        {/* Last event label — what the worker just did, plain English */}
        {phase.last_event_label && (
          <p className="text-[10px] text-muted-foreground/80 truncate">
            {phase.last_event_label}
          </p>
        )}
      </div>
    </div>
  );
}

export interface PhasedProgressCardProps {
  phases: Phase[];
  smoothedEtaSeconds?: number | null;
  retrying?: number;
  abandoned?: number;
  /** Channel id forwarded to the abandoned-chip click handler. */
  channelId?: string;
  /** Opens the existing FailedBatchPanel drill-down. Optional —
   *  the chip becomes a non-clickable badge when omitted. */
  onOpenFailedDrillDown?: (channelId: string) => void;
}

export function PhasedProgressCard({
  phases,
  smoothedEtaSeconds = null,
  retrying,
  abandoned,
  channelId,
  onOpenFailedDrillDown,
}: PhasedProgressCardProps) {
  // Always render in a stable order regardless of payload ordering so
  // the UI doesn't reshuffle row positions across polls. Phases the
  // backend doesn't include yet (e.g. wiki_maintenance still
  // ``pending`` because extraction hasn't started) get a synthetic
  // ``pending`` row so the user sees the full pipeline at a glance.
  const byName = new Map(phases.map((p) => [p.name, p]));
  const ordered: Phase[] = PHASE_ORDER.map(
    (name) => byName.get(name) ?? { name, state: "pending" as PhaseState },
  );

  const activePhase =
    ordered.find((p) => p.state === "in_flight") ??
    ordered.find((p) => p.state === "failed") ??
    ordered.find((p) => p.state === "pending") ??
    ordered[ordered.length - 1];

  return (
    <div
      data-testid="phased-progress-card"
      className="rounded-xl border border-white/10 bg-card/70 backdrop-blur px-4 py-3 space-y-3"
    >
      {/* Header — active phase summary */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-2 min-w-0 flex-1">
          <StateIcon state={activePhase.state} />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-foreground truncate">
              {PHASE_LABELS[activePhase.name] ?? activePhase.name}
            </div>
            {activePhase.state === "in_flight" && (
              <div className="text-[11px] text-muted-foreground">
                {formatEta(smoothedEtaSeconds)}
              </div>
            )}
            {activePhase.state === "done" && (
              <div className="text-[11px] text-emerald-600 dark:text-emerald-400">
                Pipeline complete
              </div>
            )}
            {activePhase.state === "failed" && (
              <div className="text-[11px] text-red-600 dark:text-red-400">
                Pipeline failed — see details
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Four phase rows */}
      <div className="space-y-1">
        {ordered.map((phase) => (
          <PhaseRow
            key={phase.name}
            phase={phase}
            isActive={phase.name === activePhase.name}
            smoothedEtaSeconds={smoothedEtaSeconds}
            retrying={retrying}
            abandoned={abandoned}
            onOpenFailedDrillDown={onOpenFailedDrillDown}
            channelId={channelId}
          />
        ))}
      </div>
    </div>
  );
}
