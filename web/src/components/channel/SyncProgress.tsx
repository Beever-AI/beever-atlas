import { useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Loader2, Brain, Users, GitBranch, XCircle, CheckCircle2, Clock } from "lucide-react";
import { cn } from "@/lib/utils";
import type { SyncState } from "@/hooks/useSync";
import type { BatchResultEntry } from "@/lib/types";
import { ActivityLog } from "./PipelineActivity";

function BatchResults({ results }: { results: BatchResultEntry[] }) {
  if (results.length === 0) {
    return (
      <div className="text-[11px] text-muted-foreground/60 py-2">
        No batch results yet...
      </div>
    );
  }

  return (
    <div className="space-y-2 max-h-[320px] overflow-y-auto">
      {results.map((batch) => {
        const isFailed = !!batch.error;
        return (
          <div
            key={batch.batch_num}
            className={cn(
              "rounded-lg border px-3 py-2.5 space-y-2",
              isFailed
                ? "border-red-500/20 bg-red-500/5"
                : "border-border bg-card",
            )}
          >
            {/* Batch header */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {isFailed ? (
                  <XCircle size={12} className="text-red-500" />
                ) : (
                  <CheckCircle2 size={12} className="text-emerald-500" />
                )}
                <span className="text-[11px] font-medium text-foreground">
                  Batch {batch.batch_num}
                </span>
              </div>
              <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
                {!isFailed && (
                  <>
                    <span className="flex items-center gap-1">
                      <Brain size={10} />
                      {batch.facts_count} facts
                    </span>
                    <span className="flex items-center gap-1">
                      <Users size={10} />
                      {batch.entities_count} entities
                    </span>
                    <span className="flex items-center gap-1">
                      <GitBranch size={10} />
                      {batch.relationships_count} rels
                    </span>
                  </>
                )}
                {batch.duration_seconds > 0 && (
                  <span className="flex items-center gap-1">
                    <Clock size={10} />
                    {batch.duration_seconds < 60
                      ? `${batch.duration_seconds.toFixed(1)}s`
                      : `${(batch.duration_seconds / 60).toFixed(1)}m`}
                  </span>
                )}
              </div>
            </div>

            {/* Error */}
            {isFailed && (
              <div className="text-[10px] text-red-600 dark:text-red-400 truncate">
                {batch.error}
              </div>
            )}

            {/* Sample facts */}
            {!isFailed && batch.sample_facts.length > 0 && (
              <div className="space-y-0.5">
                {batch.sample_facts.slice(0, 3).map((fact, i) => (
                  <div key={i} className="text-[10px] text-muted-foreground truncate pl-2 border-l border-primary/20">
                    {fact}
                  </div>
                ))}
                {batch.sample_facts.length > 3 && (
                  <div className="text-[10px] text-muted-foreground/50 pl-2">
                    +{batch.sample_facts.length - 3} more
                  </div>
                )}
              </div>
            )}

            {/* Sample entities */}
            {!isFailed && batch.sample_entities.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {batch.sample_entities.slice(0, 5).map((ent, i) => (
                  <span
                    key={i}
                    className="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] bg-primary/5 text-primary/80 border border-primary/10"
                  >
                    <span className="text-[8px] text-muted-foreground">{ent.type}</span>
                    {ent.name}
                  </span>
                ))}
                {batch.sample_entities.length > 5 && (
                  <span className="text-[9px] text-muted-foreground/50">
                    +{batch.sample_entities.length - 5}
                  </span>
                )}
              </div>
            )}
          </div>
        );
      })}
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
  { key: "embedder", label: "Embed" },
  { key: "cross_batch_validator_agent", label: "Validate" },
  { key: "persister", label: "Persist" },
];

function getStageStatus(
  stageIndex: number,
  stageKey: string,
  timings: Record<string, number>,
  currentStep: number | null | undefined,
): "done" | "active" | "pending" {
  if (timings[stageKey] !== undefined) return "done";
  // Step numbers are 1-based; stageIndex is 0-based
  if (currentStep != null && currentStep === stageIndex + 1) return "active";
  // If a later step is active, earlier steps without timings are still done
  if (currentStep != null && currentStep > stageIndex + 1) return "done";
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
  const [showDetails, setShowDetails] = useState(true);
  const [detailTab, setDetailTab] = useState<"activity" | "batches">("activity");

  const isFailed = syncState.state === "error";

  if (!isFailed && (!isSyncing || syncState.state !== "syncing")) {
    return null;
  }

  const processed = syncState.processed_messages ?? 0;
  const total = syncState.total_messages ?? 0;
  const parentMessages = syncState.parent_messages ?? total;
  const batch = syncState.current_batch ?? 0;
  const totalBatches = syncState.total_batches || (batch > 0 ? batch : 1);
  const stage = syncState.current_stage;
  const timings = syncState.stage_timings ?? {};
  const isRetrying = !isFailed && (stage?.includes("retrying") ?? false);
  const parsed = parseStage(stage);
  const errors = syncState.errors?.filter(Boolean) ?? [];
  const batchJobState = syncState.batch_job_state;
  const batchJobElapsed = syncState.batch_job_elapsed_seconds;

  // Extract model info from activity_log stage_start entries
  const activityLog = syncState.stage_details?.activity_log ?? [];
  const stageModels: Record<string, string> = {};
  for (const entry of activityLog) {
    if (entry.type === "stage_start" && entry.model) {
      stageModels[entry.agent] = entry.model;
    }
  }

  // Compute monotonic current step: under concurrency, take the max step across all in-flight
  // batch_stages values; fall back to parsing the singleton current_stage for Phase 1 runs.
  const batchStages = syncState.stage_details?.batch_stages;
  const maxStep = batchStages && Object.keys(batchStages).length > 0
    ? Math.max(...Object.values(batchStages).map((s) => parseStage(s)?.step ?? 0))
    : (parsed?.step ?? 0);
  const maxStepTotal = parsed?.total ?? 0;

  // Progress = messages already fully processed + fraction of current batch's stage progress.
  const basePct = total > 0 ? (processed / total) * 100 : 0;
  const stageBonus = maxStep && maxStepTotal && totalBatches > 0
    ? (maxStep / maxStepTotal) * (100 / totalBatches)
    : 0;
  const pct = isFailed ? Math.round(basePct) : Math.min(100, Math.round(basePct + stageBonus));

  return (
    <div className="border-b border-border bg-background">
      {/* Main progress section */}
      <div className="px-4 sm:px-6 pt-3 pb-2">
        {/* Header: status + batch + messages */}
        <div className="flex items-center justify-between mb-2.5">
          <div className="flex items-center gap-2">
            {isFailed ? (
              <AlertTriangle size={14} className="text-red-500" />
            ) : (
              <Loader2 size={14} className="animate-spin text-primary" />
            )}
            <span className={`text-sm font-medium ${isFailed ? "text-red-500" : "text-foreground"}`}>
              {isFailed ? "Sync failed" : isRetrying ? "Retrying..." : "Syncing channel"}
            </span>
            {totalBatches > 0 && (
              <span className="text-xs text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
                {batchStages && Object.keys(batchStages).length > 0
                  ? `${Object.keys(batchStages).length} in flight · ${syncState.batches_completed ?? 0} of ${totalBatches} done`
                  : `${syncState.batches_completed ?? 0} of ${totalBatches} batches`}
              </span>
            )}
          </div>
          <span className="text-xs text-muted-foreground">
            {parentMessages} messages · {pct}%
          </span>
        </div>

        {/* Error details */}
        {isFailed && errors.length > 0 && (
          <div className="rounded-md border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/20 px-3 py-2 mb-2.5">
            {errors.map((err, i) => (
              <div key={i} className="text-[11px] text-red-700 dark:text-red-300 truncate">
                {err}
              </div>
            ))}
          </div>
        )}

        {/* Pipeline stage indicators */}
        <div className="flex items-center gap-0.5 mb-2.5 overflow-x-auto">
          {PIPELINE_STAGES.map((s, i) => {
            const status = getStageStatus(i, s.key, timings, maxStep || null);
            return (
              <div key={s.key} className="flex items-center shrink-0">
                {i > 0 && (
                  <div className={`w-3 sm:w-5 h-px ${status === "pending" ? "bg-border" : isFailed && status === "active" ? "bg-red-400/40" : "bg-primary/40"}`} />
                )}
                <div className="flex items-center gap-1">
                  <div
                    className={`w-2 h-2 rounded-full shrink-0 ${
                      status === "done"
                        ? "bg-emerald-500"
                        : status === "active"
                          ? isFailed
                            ? "bg-red-500 ring-2 ring-red-500/30"
                            : "bg-primary ring-2 ring-primary/30 animate-pulse"
                          : "bg-muted-foreground/20"
                    }`}
                  />
                  <div className="flex flex-col">
                    <span
                      className={`text-[10px] sm:text-[11px] whitespace-nowrap ${
                        status === "done"
                          ? "text-emerald-600 dark:text-emerald-400"
                          : status === "active"
                            ? isFailed
                              ? "text-red-500 font-medium"
                              : "text-primary font-medium"
                            : "text-muted-foreground/40"
                      }`}
                    >
                      {s.label}
                    </span>
                    {stageModels[s.key] && (
                      <span className="text-[8px] font-mono text-muted-foreground/50 whitespace-nowrap leading-none mt-0.5">
                        {stageModels[s.key]}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Progress bar */}
        <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden mb-1">
          <div
            className={`h-full rounded-full transition-all duration-700 ease-out ${
              isFailed ? "bg-red-500" : isRetrying ? "bg-amber-500 animate-pulse" : "bg-primary"
            }`}
            style={{ width: `${Math.max(pct, 3)}%` }}
          />
        </div>

        {/* Current stage label + details toggle */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 min-w-0">
            <span className={`text-[11px] truncate ${isFailed ? "text-red-500/80" : isRetrying ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground"}`}>
              {parsed?.label || stage || (isFailed ? "Pipeline failed" : "Initializing...")}
            </span>
            {batchJobState && (
              <span className="shrink-0 inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] font-medium bg-violet-500/10 text-violet-500 border border-violet-500/20">
                <span className="w-1 h-1 rounded-full bg-violet-500 animate-pulse" />
                Batch API
                {batchJobElapsed != null && (
                  <span className="text-violet-400/70 font-mono">
                    {batchJobElapsed < 60
                      ? `${batchJobElapsed.toFixed(0)}s`
                      : `${(batchJobElapsed / 60).toFixed(1)}m`}
                  </span>
                )}
              </span>
            )}
          </div>
          <button
            onClick={() => setShowDetails(!showDetails)}
            className="flex items-center gap-0.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors shrink-0 ml-2"
          >
            {showDetails ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
            {showDetails ? "Hide" : "Details"}
          </button>
        </div>
      </div>

      {/* Expandable details with tabs */}
      {showDetails && (
        <div className="bg-muted/30 border-t border-border/50">
          {/* Tab bar */}
          <div className="flex items-center gap-0 px-4 sm:px-6 pt-1.5 border-b border-border/30">
            <button
              type="button"
              onClick={() => setDetailTab("activity")}
              className={cn(
                "px-3 py-1.5 text-[10px] font-medium uppercase tracking-wider border-b-2 transition-colors",
                detailTab === "activity"
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              Pipeline Activity
            </button>
            <button
              type="button"
              onClick={() => setDetailTab("batches")}
              className={cn(
                "px-3 py-1.5 text-[10px] font-medium uppercase tracking-wider border-b-2 transition-colors",
                detailTab === "batches"
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              Batch Results
              {(syncState.batch_results?.length ?? 0) > 0 && (
                <span className="ml-1 text-[9px] text-muted-foreground">
                  ({syncState.batch_results!.length})
                </span>
              )}
            </button>
          </div>

          {/* Tab content */}
          <div className="px-4 sm:px-6 py-2.5">
            {detailTab === "activity" && (
              <ActivityLog details={syncState.stage_details} />
            )}
            {detailTab === "batches" && (
              <BatchResults results={syncState.batch_results ?? []} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
