import { useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import type { SyncState } from "@/hooks/useSync";

interface ActivityEntry {
  type: "stage_start" | "output";
  agent: string;
  stage?: string;
  message?: string;
  details?: string[];
}

function ActivityLog({ details, timings }: { details?: Record<string, Record<string, unknown>>; timings: Record<string, number> }) {
  const log: ActivityEntry[] = (details?.activity_log as unknown as ActivityEntry[]) ?? [];

  if (log.length === 0) {
    return (
      <div className="text-[11px] text-muted-foreground/60 py-2">
        Waiting for pipeline events...
      </div>
    );
  }

  return (
    <div className="space-y-1 max-h-48 overflow-y-auto">
      {log.map((entry, i) => (
        <div key={i} className="flex items-start gap-2 text-[11px]">
          {entry.type === "stage_start" ? (
            <>
              <span className="text-primary shrink-0 mt-px">▶</span>
              <span className="text-foreground/80 font-medium">{entry.stage}</span>
              {timings[entry.agent] !== undefined && (
                <span className="text-muted-foreground/60 ml-auto shrink-0">
                  {timings[entry.agent] < 1
                    ? `${(timings[entry.agent] * 1000).toFixed(0)}ms`
                    : `${timings[entry.agent].toFixed(1)}s`}
                </span>
              )}
            </>
          ) : (
            <>
              <span className="text-emerald-500 shrink-0 mt-px">✓</span>
              <div className="min-w-0">
                <span className="text-foreground/70">{entry.message}</span>
                {entry.details && entry.details.length > 0 && (
                  <div className="mt-0.5 space-y-px">
                    {entry.details.map((d, j) => (
                      <div key={j} className="text-[10px] text-muted-foreground truncate pl-2 border-l border-border/50">
                        {d}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

interface SyncProgressProps {
  syncState: SyncState;
  isSyncing: boolean;
}

const PIPELINE_STAGES = [
  { key: "preprocessor", label: "Preprocess" },
  { key: "fact_extractor", label: "Extract Facts" },
  { key: "entity_extractor", label: "Extract Entities" },
  { key: "classifier_agent", label: "Classify" },
  { key: "embedder", label: "Embed" },
  { key: "cross_batch_validator_agent", label: "Validate" },
  { key: "persister", label: "Persist" },
];

function getStageStatus(
  stageKey: string,
  timings: Record<string, number>,
  currentStage: string | null | undefined,
): "done" | "active" | "pending" {
  if (timings[stageKey] !== undefined) return "done";
  if (currentStage?.toLowerCase().includes(stageKey.replace("_agent", "")))
    return "active";
  return "pending";
}

/** Parse "Step 3/7 — Classifying facts (LLM)" → { step: 3, total: 7, label: "Classifying facts (LLM)" } */
function parseStage(stage: string | null | undefined) {
  if (!stage) return null;
  const match = stage.match(/^Step (\d+)\/(\d+)\s*[—–-]\s*(.+)$/);
  if (match) return { step: parseInt(match[1]), total: parseInt(match[2]), label: match[3] };
  return { step: 0, total: 0, label: stage };
}

export function SyncProgress({ syncState, isSyncing }: SyncProgressProps) {
  const [showDetails, setShowDetails] = useState(false);

  if (!isSyncing || syncState.state !== "syncing") {
    return null;
  }

  const processed = syncState.processed_messages ?? 0;
  const total = syncState.total_messages ?? 0;
  const batch = syncState.current_batch ?? 0;
  const stage = syncState.current_stage;
  const timings = syncState.stage_timings ?? {};
  const isRetrying = stage?.includes("retrying") ?? false;
  const parsed = parseStage(stage);

  // Estimate total batches from what we know
  const batchSize = batch > 0 && processed > 0 ? Math.ceil(processed / batch) : 2;
  const totalBatches = total > 0 ? Math.ceil(total / batchSize) : 1;

  // Progress = messages already fully processed + fraction of current batch's stage progress.
  // This is monotonically increasing: processed only goes up, and stage adds a small bonus.
  const basePct = total > 0 ? (processed / total) * 100 : 0;
  const stageBonus = parsed?.step && parsed?.total && totalBatches > 0
    ? (parsed.step / parsed.total) * (100 / totalBatches)
    : 0;
  const pct = Math.min(100, Math.round(basePct + stageBonus));

  return (
    <div className="border-b border-border bg-background">
      {/* Main progress section */}
      <div className="px-4 sm:px-6 pt-3 pb-2">
        {/* Header: status + batch + messages */}
        <div className="flex items-center justify-between mb-2.5">
          <div className="flex items-center gap-2">
            <Loader2 size={14} className="animate-spin text-primary" />
            <span className="text-sm font-medium text-foreground">
              {isRetrying ? "Retrying..." : "Syncing channel"}
            </span>
            {batch > 0 && (
              <span className="text-xs text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
                Batch {batch}/{totalBatches}
              </span>
            )}
          </div>
          <span className="text-xs text-muted-foreground">
            {processed}/{total} messages · {pct}%
          </span>
        </div>

        {/* Pipeline stage indicators */}
        <div className="flex items-center gap-0.5 mb-2.5 overflow-x-auto">
          {PIPELINE_STAGES.map((s, i) => {
            const status = getStageStatus(s.key, timings, stage);
            return (
              <div key={s.key} className="flex items-center shrink-0">
                {i > 0 && (
                  <div className={`w-3 sm:w-5 h-px ${status === "pending" ? "bg-border" : "bg-primary/40"}`} />
                )}
                <div className="flex items-center gap-1">
                  <div
                    className={`w-2 h-2 rounded-full shrink-0 ${
                      status === "done"
                        ? "bg-emerald-500"
                        : status === "active"
                          ? "bg-primary ring-2 ring-primary/30 animate-pulse"
                          : "bg-muted-foreground/20"
                    }`}
                  />
                  <span
                    className={`text-[10px] sm:text-[11px] whitespace-nowrap ${
                      status === "done"
                        ? "text-emerald-600 dark:text-emerald-400"
                        : status === "active"
                          ? "text-primary font-medium"
                          : "text-muted-foreground/40"
                    }`}
                  >
                    {s.label}
                  </span>
                </div>
              </div>
            );
          })}
        </div>

        {/* Progress bar */}
        <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden mb-1">
          <div
            className={`h-full rounded-full transition-all duration-700 ease-out ${
              isRetrying ? "bg-amber-500 animate-pulse" : "bg-primary"
            }`}
            style={{ width: `${Math.max(pct, 3)}%` }}
          />
        </div>

        {/* Current stage label + details toggle */}
        <div className="flex items-center justify-between">
          <span className={`text-[11px] truncate ${isRetrying ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground"}`}>
            {parsed?.label || stage || "Initializing..."}
          </span>
          <button
            onClick={() => setShowDetails(!showDetails)}
            className="flex items-center gap-0.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors shrink-0 ml-2"
          >
            {showDetails ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
            {showDetails ? "Hide" : "Details"}
          </button>
        </div>
      </div>

      {/* Expandable activity log */}
      {showDetails && (
        <div className="px-4 sm:px-6 py-2.5 bg-muted/30 border-t border-border/50">
          <div className="text-[10px] font-medium text-muted-foreground uppercase tracking-wider mb-1.5">
            Pipeline Activity
          </div>
          <ActivityLog details={syncState.stage_details} timings={timings} />
        </div>
      )}
    </div>
  );
}
