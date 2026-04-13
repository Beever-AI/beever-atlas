import { useState } from "react";
import { useParams } from "react-router-dom";
import { useSyncHistory } from "@/hooks/useSyncHistory";
import {
  CheckCircle2,
  XCircle,
  Clock,
  ChevronDown,
  ChevronRight,
  RefreshCw,
  Loader2,
  Brain,
  Users,
  GitBranch,
  BarChart3,
  Timer,
  Activity,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { SyncHistoryEntry, BatchResultEntry } from "@/lib/types";
import { AgentSampleRow, ActivityLog, ExpandableText } from "./PipelineActivity";

// AgentIcon, AgentSampleRow, ActivityLog, ExpandableText are imported from PipelineActivity

function CompactBatchResults({ results }: { results: BatchResultEntry[] }) {
  const [expandedBatch, setExpandedBatch] = useState<number | null>(null);

  if (results.length === 0) return null;

  return (
    <div className="space-y-1.5">
      {results.map((batch) => {
        const isExpanded = expandedBatch === batch.batch_num;
        const hasSamples =
          (batch.sample_facts?.length ?? 0) > 0 ||
          (batch.sample_entities?.length ?? 0) > 0 ||
          (batch.sample_relationships?.length ?? 0) > 0;
        // Error rows are expandable too — users need the full traceback to diagnose.
        const isClickable = hasSamples || !!batch.error;

        return (
          <div
            key={batch.batch_num}
            className={cn(
              "rounded-md border overflow-hidden",
              batch.error ? "border-red-500/20 bg-red-500/5" : "border-border bg-card",
            )}
          >
            <button
              type="button"
              onClick={() => isClickable && setExpandedBatch(isExpanded ? null : batch.batch_num)}
              className={cn(
                "w-full px-2.5 py-1.5 flex items-center justify-between",
                isClickable && "hover:bg-muted/30 transition-colors cursor-pointer",
              )}
            >
              <div className="flex items-center gap-2">
                {batch.error ? <XCircle size={10} className="text-red-500" /> : <CheckCircle2 size={10} className="text-emerald-500" />}
                <span className="text-[11px] font-medium">Batch {batch.batch_num}</span>
              </div>
              <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
                {!batch.error && (
                  <>
                    <span className="flex items-center gap-0.5"><Brain size={9} />{batch.facts_count}</span>
                    <span className="flex items-center gap-0.5"><Users size={9} />{batch.entities_count}</span>
                    <span className="flex items-center gap-0.5"><GitBranch size={9} />{batch.relationships_count}</span>
                  </>
                )}
                {batch.duration_seconds > 0 && (
                  <span className="flex items-center gap-0.5">
                    <Clock size={9} />
                    {batch.duration_seconds < 60 ? `${batch.duration_seconds.toFixed(1)}s` : `${(batch.duration_seconds / 60).toFixed(1)}m`}
                  </span>
                )}
                {batch.error && (
                  <span
                    className="text-red-500 truncate max-w-[260px]"
                    title="Click the row to expand the full error"
                  >
                    {batch.error}
                  </span>
                )}
                {isClickable && (
                  isExpanded ? <ChevronDown size={10} /> : <ChevronRight size={10} />
                )}
              </div>
            </button>

            {isExpanded && batch.error && (
              <div className="border-t border-red-500/20 px-2.5 py-2 space-y-1 bg-red-500/5">
                <div className="text-[9px] uppercase tracking-wider text-red-500/70 flex items-center justify-between">
                  <span>Full error</span>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      navigator.clipboard?.writeText(batch.error ?? "");
                    }}
                    className="text-[9px] normal-case tracking-normal text-red-500/70 hover:text-red-500 underline-offset-2 hover:underline"
                  >
                    Copy
                  </button>
                </div>
                <pre className="text-[10px] font-mono whitespace-pre-wrap break-words leading-snug text-red-600 dark:text-red-400 max-h-[360px] overflow-y-auto">
                  {batch.error}
                </pre>
              </div>
            )}

            {isExpanded && !batch.error && (
              <div className="border-t border-border/30 px-2.5 py-2 space-y-2">
                {batch.sample_facts && batch.sample_facts.length > 0 && (
                  <div className="space-y-0.5">
                    <div className="text-[9px] uppercase tracking-wider text-muted-foreground/50 mb-1">Facts</div>
                    {batch.sample_facts.map((fact, i) => (
                      <div key={i} className="text-[10px] text-muted-foreground pl-2 border-l border-primary/20">
                        <ExpandableText text={fact} />
                      </div>
                    ))}
                  </div>
                )}

                {batch.sample_entities && batch.sample_entities.length > 0 && (
                  <div>
                    <div className="text-[9px] uppercase tracking-wider text-muted-foreground/50 mb-1">Entities</div>
                    <div className="flex flex-wrap gap-1">
                      {batch.sample_entities.map((ent, i) => (
                        <span
                          key={i}
                          className="inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] bg-primary/5 text-primary/80 border border-primary/10"
                        >
                          <span className="text-[8px] text-muted-foreground">{ent.type}</span>
                          {ent.name}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {batch.sample_relationships && batch.sample_relationships.length > 0 && (
                  <div>
                    <div className="text-[9px] uppercase tracking-wider text-muted-foreground/50 mb-1">Relationships</div>
                    <div className="space-y-0.5">
                      {batch.sample_relationships.map((rel, i) => (
                        <AgentSampleRow
                          key={i}
                          sample={{ item_type: "relationship", source: rel.source, target: rel.target, rel_type: (rel as { source: string; target: string; type: string }).type }}
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })
    + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function formatDuration(start: string | null, end: string | null): string {
  if (!start || !end) return "—";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

function getDurationSeconds(start: string | null, end: string | null): number | null {
  if (!start || !end) return null;
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  return ms / 1000;
}

function SyncJobCard({ entry }: { entry: SyncHistoryEntry }) {
  const [expanded, setExpanded] = useState(false);
  const [detailTab, setDetailTab] = useState<"activity" | "batches">("activity");

  const isFailed = entry.status === "failed";
  const isRunning = entry.status === "running";
  const activityLog = entry.stage_details?.activity_log ?? [];

  // Extract stage models
  const stageModels: Record<string, string> = {};
  for (const e of activityLog) {
    if (e.type === "stage_start" && e.model) {
      stageModels[e.agent] = e.model;
    }
  }

  const STAGES = [
    { key: "preprocessor", label: "Pre" },
    { key: "fact_extractor", label: "Facts" },
    { key: "entity_extractor", label: "Entities" },
    { key: "embedder", label: "Embed" },
    { key: "cross_batch_validator_agent", label: "Validate" },
    { key: "persister", label: "Persist" },
  ];

  return (
    <div className={cn(
      "rounded-xl border overflow-hidden bg-card/70",
      isFailed ? "border-red-500/25" : isRunning ? "border-primary/40" : "border-border",
    )}>
      {/* Header */}
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full px-5 py-4 flex items-center justify-between hover:bg-muted/30 transition-colors"
      >
        <div className="flex items-center gap-3">
          {isFailed ? (
            <XCircle size={14} className="text-red-500 shrink-0" />
          ) : isRunning ? (
            <Loader2 size={14} className="text-primary animate-spin shrink-0" />
          ) : (
            <CheckCircle2 size={14} className="text-emerald-500 shrink-0" />
          )}
          <div className="flex flex-col items-start gap-0.5">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-foreground">
                {entry.sync_type === "full" ? "Full sync" : "Incremental sync"}
              </span>
              <span className={cn(
                "text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wide",
                isFailed ? "bg-red-500/10 text-red-500" : isRunning ? "bg-primary/10 text-primary" : "bg-emerald-500/10 text-emerald-500",
              )}>
                {entry.status}
              </span>
            </div>
            <span className="text-xs text-muted-foreground">
              {formatDate(entry.started_at)} · {entry.parent_messages} msgs · {formatDuration(entry.started_at, entry.completed_at)}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {/* Mini stage dots */}
          <div className="hidden sm:flex items-center gap-0.5">
            {STAGES.map((s) => {
              const done = (entry.stage_timings ?? {})[s.key] !== undefined;
              return (
                <div
                  key={s.key}
                  className={cn(
                    "w-1.5 h-1.5 rounded-full",
                    done ? "bg-emerald-500" : isFailed ? "bg-red-500/30" : "bg-muted-foreground/20",
                  )}
                  title={`${s.label}${stageModels[s.key] ? ` (${stageModels[s.key]})` : ""}`}
                />
              );
            })}
          </div>
          <div className="h-6 w-6 rounded-md border border-border/70 bg-background/40 flex items-center justify-center">
            {expanded ? <ChevronDown size={14} className="text-muted-foreground" /> : <ChevronRight size={14} className="text-muted-foreground" />}
          </div>
        </div>
      </button>

      {/* Expanded details */}
      {expanded && (
        <div className="border-t border-border/50 bg-muted/15">
          {/* Stage timing bar */}
          <div className="px-5 py-3 flex items-center gap-2 overflow-x-auto border-b border-border/30">
            {STAGES.map((s, i) => {
              const timing = (entry.stage_timings ?? {})[s.key];
              const done = timing !== undefined;
              return (
                <div key={s.key} className="flex items-center shrink-0">
                  {i > 0 && <div className={`w-3 h-px ${done ? "bg-emerald-500/40" : "bg-border/70"}`} />}
                  <div className="flex flex-col items-center gap-0.5 min-w-[72px] rounded-md px-2 py-1 bg-background/40 border border-border/50">
                    <div className="flex items-center gap-1">
                      <div className={`w-1.5 h-1.5 rounded-full ${done ? "bg-emerald-500" : "bg-muted-foreground/20"}`} />
                      <span className={`text-[10px] whitespace-nowrap ${done ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground/40"}`}>
                        {s.label}
                      </span>
                    </div>
                    {stageModels[s.key] && (
                      <span className="text-[7px] font-mono text-muted-foreground/40 whitespace-nowrap">
                        {stageModels[s.key]}
                      </span>
                    )}
                    {done && timing != null && (
                      <span className="text-[8px] font-mono text-muted-foreground/50">
                        {timing < 1 ? `${(timing * 1000).toFixed(0)}ms` : `${timing.toFixed(1)}s`}
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Errors — show full traceback, wrap long lines, allow copy */}
          {isFailed && entry.errors.length > 0 && (
            <div className="px-5 py-3 border-b border-border/30 space-y-2">
              {entry.errors.filter(Boolean).map((err, i) => (
                <div
                  key={i}
                  className="rounded-md border border-red-500/20 bg-red-500/5 px-2.5 py-2 space-y-1"
                >
                  <div className="flex items-center justify-between">
                    <span className="text-[9px] uppercase tracking-wider text-red-500/70">
                      Error {i + 1}
                    </span>
                    <button
                      type="button"
                      onClick={() => navigator.clipboard?.writeText(err)}
                      className="text-[9px] text-red-500/70 hover:text-red-500 hover:underline underline-offset-2"
                    >
                      Copy
                    </button>
                  </div>
                  <pre className="text-[11px] font-mono whitespace-pre-wrap break-words leading-snug text-red-600 dark:text-red-400 max-h-[320px] overflow-y-auto">
                    {err}
                  </pre>
                </div>
              ))}
            </div>
          )}

          {/* Tabs */}
          <div className="flex items-center gap-0 px-5 pt-1 border-b border-border/30">
            <button
              type="button"
              onClick={() => setDetailTab("activity")}
              className={cn(
                "px-3 py-1.5 text-[10px] font-medium uppercase tracking-wider border-b-2 transition-colors",
                detailTab === "activity" ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              Pipeline Activity
            </button>
            <button
              type="button"
              onClick={() => setDetailTab("batches")}
              className={cn(
                "px-3 py-1.5 text-[10px] font-medium uppercase tracking-wider border-b-2 transition-colors",
                detailTab === "batches" ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              Batches {entry.batch_results.length > 0 && `(${entry.batch_results.length})`}
            </button>
          </div>

          <div className="px-5 py-3">
            {detailTab === "activity" && <ActivityLog details={entry.stage_details} />}
            {detailTab === "batches" && <CompactBatchResults results={entry.batch_results} />}
          </div>
        </div>
      )}
    </div>
  );
}

export function SyncHistoryTab() {
  const { id } = useParams<{ id: string }>();
  const { entries, loading, error, refresh } = useSyncHistory(id ?? "");
  const completedRuns = entries.filter((e) => e.status === "completed").length;
  const failedRuns = entries.filter((e) => e.status === "failed").length;
  const totalMessages = entries.reduce((sum, e) => sum + (e.parent_messages ?? 0), 0);
  const avgDurationSeconds = (() => {
    const durations = entries
      .map((e) => getDurationSeconds(e.started_at, e.completed_at))
      .filter((n): n is number => typeof n === "number");
    if (durations.length === 0) return null;
    return durations.reduce((a, b) => a + b, 0) / durations.length;
  })();

  return (
    <div className="h-full overflow-auto p-4 sm:p-6 lg:p-8 space-y-5 max-w-7xl mx-auto w-full">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold text-foreground">Sync History</h2>
          <p className="text-sm text-muted-foreground">Detailed pipeline progress and outcomes for each channel sync run</p>
        </div>
        <button
          onClick={refresh}
          disabled={loading}
          className="inline-flex items-center gap-2 px-3.5 py-2 rounded-lg text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-50 border border-border/60"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>
      </div>

      {entries.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div className="rounded-xl border border-border bg-card/70 px-4 py-3">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">Runs</div>
            <div className="mt-1 flex items-center gap-2">
              <BarChart3 size={14} className="text-primary" />
              <span className="text-xl font-semibold text-foreground">{entries.length}</span>
            </div>
          </div>
          <div className="rounded-xl border border-border bg-card/70 px-4 py-3">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">Success / Failed</div>
            <div className="mt-1 flex items-center gap-2 text-sm">
              <span className="text-emerald-500 font-semibold">{completedRuns}</span>
              <span className="text-muted-foreground">/</span>
              <span className="text-red-500 font-semibold">{failedRuns}</span>
            </div>
          </div>
          <div className="rounded-xl border border-border bg-card/70 px-4 py-3">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">Messages Processed</div>
            <div className="mt-1 flex items-center gap-2">
              <Activity size={14} className="text-primary" />
              <span className="text-xl font-semibold text-foreground">{totalMessages.toLocaleString()}</span>
            </div>
          </div>
          <div className="rounded-xl border border-border bg-card/70 px-4 py-3">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">Avg Runtime</div>
            <div className="mt-1 flex items-center gap-2">
              <Timer size={14} className="text-primary" />
              <span className="text-xl font-semibold text-foreground">
                {avgDurationSeconds == null
                  ? "—"
                  : avgDurationSeconds < 60
                    ? `${avgDurationSeconds.toFixed(1)}s`
                    : `${(avgDurationSeconds / 60).toFixed(1)}m`}
              </span>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/20 px-3 py-2 text-sm text-red-700 dark:text-red-300">
          {error}
        </div>
      )}

      {loading && entries.length === 0 ? (
        <div className="rounded-xl border border-border bg-card/50 py-16 flex items-center justify-center text-muted-foreground/60">
          <div className="flex items-center gap-2">
            <Loader2 size={18} className="animate-spin" />
            <span className="text-sm">Loading sync history…</span>
          </div>
        </div>
      ) : entries.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border bg-card/30 text-center py-16 text-muted-foreground/70 text-sm">
          <p className="text-foreground font-medium mb-1">No sync records yet</p>
          <p>Run sync for this channel to populate pipeline history and stage details.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {entries.map((entry) => (
            <SyncJobCard key={entry.job_id} entry={entry} />
          ))}
        </div>
      )}
    </div>
  );
}
