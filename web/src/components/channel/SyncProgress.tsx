import { useEffect, useMemo, useRef, useState } from "react";
import {
  Brain,
  Users,
  GitBranch,
  XCircle,
  CheckCircle2,
  Clock,
  Sparkles,
  Image as ImageIcon,
  ChevronDown,
  ChevronRight,
  Loader2,
  Search,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { SyncState } from "@/hooks/useSync";
import type { BatchResultEntry } from "@/lib/types";
import { SyncProgressV2 } from "./SyncProgressV2";

type BatchFilter = "all" | "done" | "running" | "pending" | "failed";

function inferState(b: BatchResultEntry): "pending" | "running" | "done" | "failed" {
  if (b.state) return b.state;
  if (b.error) return "failed";
  if (
    b.facts_count > 0 ||
    b.entities_count > 0 ||
    b.duration_seconds > 0 ||
    (b.embedded_count ?? 0) > 0
  ) {
    return "done";
  }
  return "pending";
}

function useLocalFilter(): [BatchFilter, (v: BatchFilter) => void] {
  const KEY = "beever.monitor.batchResultsFilter";
  const [filter, setFilter] = useState<BatchFilter>(() => {
    if (typeof window === "undefined") return "all";
    try {
      const raw = window.localStorage.getItem(KEY);
      if (
        raw === '"all"' ||
        raw === '"done"' ||
        raw === '"running"' ||
        raw === '"pending"' ||
        raw === '"failed"'
      ) {
        return JSON.parse(raw) as BatchFilter;
      }
    } catch {
      /* ignore */
    }
    return "all";
  });
  const setPersistent = (v: BatchFilter) => {
    setFilter(v);
    try {
      window.localStorage.setItem(KEY, JSON.stringify(v));
    } catch {
      /* ignore */
    }
  };
  return [filter, setPersistent];
}

export function BatchResults({ results }: { results: BatchResultEntry[] }) {
  const [filter, setFilter] = useLocalFilter();
  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({});
  const [searchTerm, setSearchTerm] = useState<string>("");
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  // Cmd-K / Ctrl-K: focus the search box. Esc when focused: clear + blur.
  // Only intercepts when the BatchResults panel is mounted (active tab),
  // so the activity-tab search shortcut never collides.
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

  // Annotate every entry with its inferred state once so filter +
  // grouping work on a stable shape.
  const annotated = useMemo(
    () => results.map((r) => ({ ...r, _state: inferState(r) })),
    [results],
  );

  // Per-state counts for the filter chip badges.
  const counts = useMemo(() => {
    const c = { all: annotated.length, done: 0, running: 0, pending: 0, failed: 0 };
    for (const r of annotated) c[r._state] += 1;
    return c;
  }, [annotated]);

  const filtered = useMemo(() => {
    let ordered = [...annotated].sort((a, b) => b.batch_num - a.batch_num);
    if (filter !== "all") {
      ordered = ordered.filter((r) => r._state === filter);
    }
    const q = searchTerm.trim().toLowerCase();
    if (q) {
      ordered = ordered.filter((r) => {
        if (String(r.batch_num) === q) return true;
        const hay = [
          ...r.sample_facts,
          ...r.sample_entities.map((e) => `${e.type} ${e.name}`),
          ...r.sample_relationships.map(
            (rel) => `${rel.source} ${rel.type} ${rel.target}`,
          ),
          r.error ?? "",
        ]
          .join(" ")
          .toLowerCase();
        return hay.includes(q);
      });
    }
    return ordered;
  }, [annotated, filter, searchTerm]);

  if (results.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-8 text-center gap-1.5">
        <Loader2 size={16} className="text-muted-foreground/40" />
        <div className="text-[12px] font-medium text-foreground/80">
          No batch results yet
        </div>
        <div className="text-[10.5px] text-muted-foreground/70 max-w-md">
          Each batch's facts, entities, and relationships will appear here
          as it completes processing.
        </div>
      </div>
    );
  }

  const FILTER_CHIPS: Array<{ key: BatchFilter; label: string; color: string; icon: string }> = [
    { key: "all", label: "All", color: "text-foreground", icon: "·" },
    { key: "done", label: "Done", color: "text-emerald-500", icon: "✓" },
    { key: "running", label: "Running", color: "text-primary", icon: "●" },
    { key: "pending", label: "Pending", color: "text-muted-foreground/60", icon: "○" },
    { key: "failed", label: "Failed", color: "text-red-500", icon: "✗" },
  ];

  return (
    <div className="space-y-2 max-h-[520px] overflow-y-auto">
      {/* Sticky header bundle: search input + filter chips. */}
      <div className="sticky top-0 z-10 bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/80 pb-1 border-b border-border pt-0.5">
      {/* Text search — matches batch number, facts, entities, relationships, errors. */}
      <div className="flex items-center gap-2 px-0.5 pb-1">
        <Search size={12} className="text-muted-foreground/60 shrink-0" />
        <input
          ref={searchInputRef}
          type="search"
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          placeholder="Search batches… (#7, fact text, entity name) — ⌘K"
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
          {filtered.length} {filtered.length === 1 ? "batch" : "batches"}
        </span>
      </div>
      {/* Filter chips with state counts */}
      <div className="flex flex-wrap items-center gap-1">
        {FILTER_CHIPS.map((f) => {
          const count = counts[f.key];
          const active = filter === f.key;
          const disabled = count === 0 && f.key !== "all";
          return (
            <button
              key={f.key}
              type="button"
              disabled={disabled}
              onClick={() => setFilter(f.key)}
              className={cn(
                "inline-flex items-center gap-1.5 px-2 py-0.5 text-[10px] rounded transition-colors",
                active
                  ? "bg-primary/10 text-primary border border-primary/20"
                  : "text-muted-foreground hover:text-foreground hover:bg-muted/40 border border-transparent",
                disabled && "opacity-40 cursor-not-allowed",
              )}
            >
              <span className={cn("font-mono", f.color)}>{f.icon}</span>
              <span className="font-medium uppercase tracking-wide">{f.label}</span>
              <span className="text-[9px] tabular-nums text-muted-foreground/70">
                {count}
              </span>
            </button>
          );
        })}
      </div>
      </div>

      {filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-8 text-center gap-1.5">
          <div className="text-[12px] font-medium text-foreground/80">
            No batches in this state
          </div>
          <div className="text-[10.5px] text-muted-foreground/70">
            Try a different filter above.
          </div>
        </div>
      ) : (
        filtered.map((batch) => {
        const state = batch._state;
        const isFailed = state === "failed";
        const isCollapsed = collapsed[batch.batch_num] ?? false;
        const hasContent =
          batch.sample_facts.length > 0 ||
          batch.sample_entities.length > 0 ||
          batch.sample_relationships.length > 0 ||
          batch.facts_count > 0 ||
          batch.entities_count > 0;
        return (
          <div
            key={batch.batch_num}
            className={cn(
              "rounded-lg border overflow-hidden",
              isFailed
                ? "border-red-500/20 bg-red-500/5"
                : state === "running"
                  ? "border-primary/30 bg-primary/[0.02]"
                  : state === "pending"
                    ? "border-border bg-muted/10"
                    : "border-border bg-card",
            )}
          >
            {/* Card header — always visible, clickable to collapse */}
            <button
              type="button"
              onClick={() =>
                setCollapsed((prev) => ({
                  ...prev,
                  [batch.batch_num]: !isCollapsed,
                }))
              }
              className="w-full flex items-center justify-between px-3 py-2 hover:bg-muted/20 transition-colors text-left"
            >
              <div className="flex items-center gap-2">
                <span className="text-muted-foreground/60 shrink-0">
                  {isCollapsed ? (
                    <ChevronRight size={13} />
                  ) : (
                    <ChevronDown size={13} />
                  )}
                </span>
                {isFailed ? (
                  <XCircle size={13} className="text-red-500" />
                ) : state === "running" ? (
                  <Loader2 size={13} className="text-primary animate-spin" />
                ) : state === "pending" ? (
                  <span className="w-[13px] h-[13px] inline-flex items-center justify-center text-muted-foreground/60 text-[14px] leading-none">○</span>
                ) : (
                  <CheckCircle2 size={13} className="text-emerald-500" />
                )}
                <span className="text-[12px] font-semibold text-foreground">
                  Batch {batch.batch_num}
                </span>
                <span
                  className={cn(
                    "text-[9px] uppercase tracking-wider font-medium px-1.5 py-0.5 rounded",
                    state === "done" && "text-emerald-600 bg-emerald-500/10",
                    state === "running" && "text-primary bg-primary/10",
                    state === "pending" && "text-muted-foreground bg-muted/40",
                    state === "failed" && "text-red-500 bg-red-500/10",
                  )}
                >
                  {state}
                </span>
              </div>
              <div className="flex items-center gap-3 text-[10px] text-muted-foreground">
                {!isFailed && (
                  <>
                    <span className="flex items-center gap-1" title="Facts extracted">
                      <Brain size={11} className="text-violet-500" />
                      <span className="font-semibold tabular-nums text-foreground">
                        {batch.facts_count}
                      </span>
                      <span>facts</span>
                    </span>
                    <span className="flex items-center gap-1" title="Entities found">
                      <Users size={11} className="text-emerald-500" />
                      <span className="font-semibold tabular-nums text-foreground">
                        {batch.entities_count}
                      </span>
                      <span>entities</span>
                    </span>
                    <span className="flex items-center gap-1" title="Relationships">
                      <GitBranch size={11} className="text-sky-500" />
                      <span className="font-semibold tabular-nums text-foreground">
                        {batch.relationships_count}
                      </span>
                      <span>rels</span>
                    </span>
                    {(batch.embedded_count ?? 0) > 0 && (
                      <span className="flex items-center gap-1" title="Facts embedded">
                        <Sparkles size={11} className="text-amber-500" />
                        <span className="font-semibold tabular-nums text-foreground">
                          {batch.embedded_count}
                        </span>
                        <span>embedded</span>
                      </span>
                    )}
                    {(batch.media_count ?? 0) > 0 && (
                      <span className="flex items-center gap-1" title="Media analyzed">
                        <ImageIcon size={11} className="text-sky-500" />
                        <span className="font-semibold tabular-nums text-foreground">
                          {batch.media_count}
                        </span>
                        <span>media</span>
                      </span>
                    )}
                  </>
                )}
                {batch.duration_seconds > 0 && (
                  <span className="flex items-center gap-1" title="Total batch duration">
                    <Clock size={11} />
                    <span className="font-mono tabular-nums">
                      {batch.duration_seconds < 60
                        ? `${batch.duration_seconds.toFixed(1)}s`
                        : `${(batch.duration_seconds / 60).toFixed(1)}m`}
                    </span>
                  </span>
                )}
              </div>
            </button>

            {/* Body — collapsible */}
            {!isCollapsed && (
              <div className="px-3 pb-2.5 space-y-2.5 border-t border-border/40">
                {/* Error */}
                {isFailed && batch.error && (
                  <div className="text-[10.5px] text-red-600 dark:text-red-400 break-words mt-2">
                    {batch.error}
                  </div>
                )}

                {/* State-aware empty body when there's no content */}
                {!isFailed && !hasContent && (
                  <div className="mt-2 text-[10.5px] text-muted-foreground/70">
                    {state === "pending" && (
                      <>This batch hasn't started yet — waiting for an available worker slot.</>
                    )}
                    {state === "running" && (
                      <>Extracting facts and entities — results will appear here as soon as the persister stage completes.</>
                    )}
                    {state === "done" && (
                      <>
                        Batch completed but produced no extractable knowledge.
                        Possible reasons: messages were system events, brief
                        acknowledgements, or media-only content with no
                        transcribable signal.
                      </>
                    )}
                  </div>
                )}

                {/* Sample facts */}
                {!isFailed && batch.sample_facts.length > 0 && (
                  <div className="space-y-1 mt-2">
                    <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-wider text-muted-foreground/70 font-medium">
                      <Brain size={9} className="text-violet-500" />
                      Sample facts
                    </div>
                    <div className="space-y-0.5">
                      {batch.sample_facts.slice(0, 5).map((fact, i) => (
                        <div
                          key={i}
                          className="text-[10.5px] leading-snug text-foreground/80 pl-2 border-l-2 border-violet-500/30"
                        >
                          {fact}
                        </div>
                      ))}
                      {batch.sample_facts.length > 5 && (
                        <div className="text-[10px] text-muted-foreground/60 pl-2">
                          +{batch.sample_facts.length - 5} more facts
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {/* Sample entities */}
                {!isFailed && batch.sample_entities.length > 0 && (
                  <div className="space-y-1">
                    <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-wider text-muted-foreground/70 font-medium">
                      <Users size={9} className="text-emerald-500" />
                      Sample entities
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {batch.sample_entities.slice(0, 10).map((ent, i) => (
                        <span
                          key={i}
                          className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] bg-emerald-500/5 text-foreground border border-emerald-500/15"
                        >
                          <span className="text-[8.5px] uppercase tracking-wider text-emerald-600/80 dark:text-emerald-400/80 font-medium">
                            {ent.type || "?"}
                          </span>
                          <span className="text-foreground/85">{ent.name}</span>
                        </span>
                      ))}
                      {batch.sample_entities.length > 10 && (
                        <span className="text-[10px] text-muted-foreground/60 self-center">
                          +{batch.sample_entities.length - 10} more
                        </span>
                      )}
                    </div>
                  </div>
                )}

                {/* Sample relationships */}
                {!isFailed && batch.sample_relationships.length > 0 && (
                  <div className="space-y-1">
                    <div className="flex items-center gap-1.5 text-[9px] uppercase tracking-wider text-muted-foreground/70 font-medium">
                      <GitBranch size={9} className="text-sky-500" />
                      Sample relationships
                    </div>
                    <div className="space-y-0.5">
                      {batch.sample_relationships.slice(0, 5).map((r, i) => (
                        <div
                          key={i}
                          className="text-[10px] flex items-center gap-1.5 flex-wrap pl-2"
                        >
                          <span className="font-medium text-foreground/85">
                            {r.source}
                          </span>
                          <span className="text-[8.5px] uppercase tracking-wider text-sky-600/80 dark:text-sky-400/80 font-medium px-1 py-0.5 rounded bg-sky-500/5">
                            {r.type || "→"}
                          </span>
                          <span className="font-medium text-foreground/85">
                            {r.target}
                          </span>
                        </div>
                      ))}
                      {batch.sample_relationships.length > 5 && (
                        <div className="text-[10px] text-muted-foreground/60 pl-2">
                          +{batch.sample_relationships.length - 5} more
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })
      )}
    </div>
  );
}

interface SyncProgressProps {
  syncState: SyncState;
  isSyncing: boolean;
  channelId?: string | null;
  /** Optional controlled collapse state — when provided, the parent
   *  layout can react to the collapsed state (e.g. switch fullscreen
   *  → compact). Without these props, the monitor keeps its internal
   *  localStorage-backed collapse state as before. */
  collapsed?: boolean;
  onCollapsedChange?: (next: boolean) => void;
}

export function SyncProgress({
  syncState,
  isSyncing,
  channelId,
  collapsed,
  onCollapsedChange,
}: SyncProgressProps) {
  const isFailed = syncState.state === "error";
  if (!channelId) return null;
  const phases = syncState.phases ?? [];
  // Render whenever there's pipeline activity to show — that's either
  // an active sync (state="syncing") OR any phase still in_flight under
  // the decoupled flow (background worker still processing extraction
  // or wiki_maintenance after the HTTP sync has already returned).
  //
  // Also render with empty phases when ``isSyncing`` is true — the user
  // just clicked "Sync Channel" and we want the monitor to appear
  // immediately, before the first /sync/status poll returns phases.
  // SyncProgressV2 handles the empty-phases case with a "starting"
  // placeholder so the user has feedback right away.
  const anyPhaseInFlight = phases.some((p) => p.state === "in_flight");
  const isActive =
    isFailed || isSyncing || syncState.state === "syncing" || anyPhaseInFlight;
  if (!isActive) return null;
  if (phases.length === 0 && !isSyncing && syncState.state !== "syncing") {
    return null;
  }
  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <SyncProgressV2
        channelId={channelId}
        phases={phases}
        state={syncState.state}
        events={syncState.recent_events ?? []}
        stageDetails={syncState.stage_details}
        batchResults={syncState.batch_results}
        // ``batchResultsJobId`` is the job_id of the row that supplied
        // ``batch_results`` (the most-recent ``/sync/status`` response).
        // ``currentJobId`` is the authoritative current-sync id from the
        // trigger. SyncProgressV2 gates ``batch_results`` ingestion on
        // ``batchResultsJobId === currentJobId`` so stale rows from the
        // previous run can't leak DONE chips into the new view.
        batchResultsJobId={syncState.job_id ?? null}
        currentJobId={syncState.triggered_job_id ?? syncState.job_id ?? null}
        smoothedEtaSeconds={syncState.smoothed_eta_seconds ?? null}
        parseFailureState={syncState.parse_failure_state ?? null}
        totalMessages={syncState.total_messages}
        processedMessages={syncState.processed_messages}
        totalBatches={syncState.total_batches}
        batchesCompleted={syncState.batches_completed}
        startedAt={syncState.started_at ?? null}
        retrying={syncState.retrying}
        abandoned={syncState.abandoned}
        collapsed={collapsed}
        onCollapsedChange={onCollapsedChange}
      />
    </div>
  );
}
