/**
 * SyncMonitor — verbose three-pane live monitor for channel sync.
 *
 * Replaces the four-dot PhasedProgressCard for the
 * unified-llm-wiki-graph-redesign change. Three panes:
 *
 *   - Left   "Message Stream"  — raw messages currently being processed.
 *   - Center "Agent Activity"  — LED strip of ingestion chain.
 *   - Right  "Wiki Updates"    — live feed of work product
 *                                (page updates, parse failures).
 *
 * Plus a stats footer with smoothed ETA, throughput, and parse-failure
 * count. Falls back gracefully when ``recent_events`` is empty or
 * the new event types aren't yet emitted.
 */

import { useMemo } from "react";
import {
  AlertCircle,
  CheckCircle2,
  FileText,
  Loader2,
  MessageSquare,
  Activity,
  Sparkles,
} from "lucide-react";
import { cn } from "@/lib/utils";

// Mirror of the backend Event shape.
export interface SyncMonitorEvent {
  ts: string;
  stage: string;
  label: string;
  event_type?: string;
  payload?: Record<string, unknown> | null;
}

export interface ParseFailureState {
  count_last_10_min: number;
  threshold: number;
  should_show_banner: boolean;
}

export interface SyncMonitorProps {
  channelId: string;
  events: SyncMonitorEvent[];
  smoothedEtaSeconds?: number | null;
  parseFailureState?: ParseFailureState | null;
  totalMessages?: number;
  processedMessages?: number;
  isSyncing?: boolean;
}

const AGENTS = [
  { key: "fact_extractor", label: "Facts" },
  { key: "entity_extractor", label: "Entities" },
  { key: "coreference_resolver", label: "Coref" },
  { key: "embedder", label: "Embed" },
  { key: "persister", label: "Persist" },
  { key: "wiki_maintainer", label: "Wiki" },
];

function fmtRelative(ts: string): string {
  try {
    const t = new Date(ts).getTime();
    const diffMs = Date.now() - t;
    const sec = Math.floor(diffMs / 1000);
    if (sec < 5) return "just now";
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    return `${Math.floor(min / 60)}h ago`;
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

interface AgentLEDState {
  state: "idle" | "running" | "done" | "failed";
  elapsedMs?: number;
  lastTs?: string;
}

function deriveAgentStates(events: SyncMonitorEvent[]): Record<string, AgentLEDState> {
  // Walk events newest-first; first agent_state per agent wins.
  const map: Record<string, AgentLEDState> = {};
  for (const evt of events) {
    if (evt.event_type !== "agent_state") continue;
    const agent = String(evt.payload?.agent ?? "");
    if (!agent || map[agent]) continue;
    const state = String(evt.payload?.state ?? "idle") as AgentLEDState["state"];
    map[agent] = {
      state: state === "running" || state === "done" || state === "failed" ? state : "idle",
      elapsedMs: typeof evt.payload?.elapsed_ms === "number" ? (evt.payload.elapsed_ms as number) : undefined,
      lastTs: evt.ts,
    };
  }
  return map;
}

export function SyncMonitor({
  channelId,
  events,
  smoothedEtaSeconds,
  parseFailureState,
  totalMessages,
  processedMessages,
  isSyncing,
}: SyncMonitorProps) {
  const messageEvents = useMemo(
    () => events.filter((e) => e.event_type === "message_processing").slice(0, 30),
    [events],
  );

  const wikiEvents = useMemo(
    () =>
      events.filter(
        (e) => e.event_type === "wiki_update" || e.event_type === "parse_failure",
      ).slice(0, 30),
    [events],
  );

  const agentStates = useMemo(() => deriveAgentStates(events), [events]);

  // Throughput: count message_processing events in the last 60s.
  const throughput = useMemo(() => {
    const cutoff = Date.now() - 60_000;
    const recent = events.filter((e) => {
      if (e.event_type !== "message_processing") return false;
      try {
        return new Date(e.ts).getTime() >= cutoff;
      } catch {
        return false;
      }
    });
    return recent.length;
  }, [events]);

  const showParseFailureBanner =
    parseFailureState?.should_show_banner ?? false;

  // Derive a human-readable current-phase label from the newest events.
  // The phase pill replaces the legacy PhasedProgressCard's role —
  // operators get one clear "what's happening right now" string.
  const currentPhase = useMemo<string>(() => {
    if (!isSyncing) return "Sync complete";
    const runningAgents: string[] = [];
    for (const evt of events) {
      if (evt.event_type !== "agent_state") continue;
      const agent = String(evt.payload?.agent ?? "");
      const state = String(evt.payload?.state ?? "");
      if (state === "running" && !runningAgents.includes(agent)) {
        runningAgents.push(agent);
      }
    }
    if (runningAgents.length === 0) return "Fetching messages…";
    // Map agent key → friendly phase name.
    const phaseLabels: Record<string, string> = {
      fact_extractor: "Extracting facts",
      entity_extractor: "Extracting entities",
      coreference_resolver: "Resolving references",
      embedder: "Generating embeddings",
      persister: "Saving to memory",
      wiki_maintainer: "Updating wiki pages",
    };
    // Prefer the "highest" agent in the pipeline (wiki_maintainer > persister > ... > fact_extractor)
    const order = [
      "wiki_maintainer",
      "persister",
      "embedder",
      "coreference_resolver",
      "entity_extractor",
      "fact_extractor",
    ];
    for (const agent of order) {
      if (runningAgents.includes(agent)) return phaseLabels[agent] ?? agent;
    }
    return "Processing…";
  }, [events, isSyncing]);

  // Percentage progress for the slim header progress bar.
  const pct = useMemo<number>(() => {
    const total = totalMessages ?? 0;
    const processed = processedMessages ?? 0;
    if (total <= 0) return 0;
    return Math.min(100, Math.round((processed / total) * 100));
  }, [totalMessages, processedMessages]);

  return (
    <div
      className="rounded-lg border border-border bg-card overflow-hidden"
      data-testid={`sync-monitor-${channelId}`}
    >
      {/* Header strip — combines status, phase, count, ETA, and progress bar
          so the legacy PhasedProgressCard isn't needed. */}
      <div className="border-b border-border bg-muted/30 px-3 py-2.5">
        <div className="flex items-center gap-2 flex-wrap">
          {isSyncing ? (
            <Loader2 size={14} className="animate-spin text-primary" />
          ) : (
            <CheckCircle2 size={14} className="text-emerald-500" />
          )}
          <span className="text-sm font-medium text-foreground">
            {isSyncing ? "Syncing channel" : "Sync complete"}
          </span>
          {isSyncing && (
            <span
              className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-primary/10 text-primary border border-primary/20"
              data-testid="sync-phase-pill"
            >
              {currentPhase}
            </span>
          )}
          <span className="text-xs text-muted-foreground ml-auto">
            <span className="font-medium text-foreground">{processedMessages ?? 0}</span>
            <span className="mx-1 text-muted-foreground/60">/</span>
            <span>{totalMessages ?? 0} messages</span>
            {pct > 0 && (
              <span className="ml-2 text-muted-foreground/70">· {pct}%</span>
            )}
            {isSyncing && smoothedEtaSeconds != null && smoothedEtaSeconds >= 0 && (
              <span className="ml-2 text-muted-foreground/70">
                · ETA {fmtEta(smoothedEtaSeconds)}
              </span>
            )}
          </span>
        </div>
        {/* Slim progress bar */}
        {totalMessages != null && totalMessages > 0 && (
          <div className="mt-2 h-1 rounded-full bg-muted/60 overflow-hidden">
            <div
              className={cn(
                "h-full rounded-full transition-all duration-500 ease-out",
                isSyncing ? "bg-primary" : "bg-emerald-500",
              )}
              style={{ width: `${pct}%` }}
            />
          </div>
        )}
      </div>

      {/* Optional parse-failure banner */}
      {showParseFailureBanner && parseFailureState && (
        <div
          role="alert"
          className="flex items-start gap-2 border-b border-amber-200 dark:border-amber-900 bg-amber-50/60 dark:bg-amber-950/30 px-3 py-2"
          data-testid="parse-failure-banner"
        >
          <AlertCircle size={14} className="shrink-0 mt-0.5 text-amber-600 dark:text-amber-400" />
          <div className="text-xs text-amber-800 dark:text-amber-200">
            {parseFailureState.count_last_10_min} wiki update
            {parseFailureState.count_last_10_min === 1 ? "" : "s"} failed in the last 10 minutes.
          </div>
        </div>
      )}

      {/* Three-pane grid */}
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_auto_1fr] gap-px bg-border min-h-[180px]">
        {/* Left — Message Stream */}
        <section
          className="bg-card p-3 overflow-hidden"
          data-testid="sync-monitor-pane-messages"
        >
          <div className="flex items-center gap-1.5 mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            <MessageSquare size={11} /> Message Stream
          </div>
          {messageEvents.length === 0 ? (
            <div className="text-xs text-muted-foreground/60 italic">
              {isSyncing
                ? "Waiting for the first message…"
                : "Idle — no messages currently being processed."}
            </div>
          ) : (
            <ul className="space-y-1 max-h-44 overflow-y-auto text-xs">
              {messageEvents.map((evt, idx) => (
                <li
                  key={`${evt.ts}-${idx}`}
                  className="border-l-2 border-primary/30 pl-2 py-0.5"
                >
                  <div className="text-foreground/80 truncate">
                    {String(evt.payload?.text_preview ?? evt.label)}
                  </div>
                  <div className="text-[10px] text-muted-foreground/70">
                    {String(evt.payload?.author ?? "")} · {fmtRelative(evt.ts)}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* Center — Agent Activity */}
        <section
          className="bg-card p-3 lg:min-w-[240px]"
          data-testid="sync-monitor-pane-agents"
        >
          <div className="flex items-center gap-1.5 mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            <Activity size={11} /> Agent Activity
          </div>
          <ul className="space-y-1.5">
            {AGENTS.map((agent) => {
              const a = agentStates[agent.key];
              const state = a?.state ?? "idle";
              return (
                <li
                  key={agent.key}
                  className="flex items-center gap-2 text-xs"
                  data-testid={`agent-led-${agent.key}`}
                >
                  <span
                    className={cn(
                      "w-2 h-2 rounded-full shrink-0",
                      state === "running" && "bg-primary animate-pulse",
                      state === "done" && "bg-emerald-500",
                      state === "failed" && "bg-rose-500",
                      state === "idle" && "bg-muted-foreground/30",
                    )}
                  />
                  <span
                    className={cn(
                      "flex-1 min-w-0",
                      state === "idle" ? "text-muted-foreground/60" : "text-foreground/80",
                    )}
                  >
                    {agent.label}
                  </span>
                  {a?.elapsedMs != null && state !== "idle" && (
                    <span className="text-[10px] font-mono text-muted-foreground">
                      {a.elapsedMs < 1000
                        ? `${a.elapsedMs}ms`
                        : `${(a.elapsedMs / 1000).toFixed(1)}s`}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </section>

        {/* Right — Wiki Updates */}
        <section
          className="bg-card p-3 overflow-hidden"
          data-testid="sync-monitor-pane-wiki"
        >
          <div className="flex items-center gap-1.5 mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            <Sparkles size={11} /> Wiki Updates
          </div>
          {wikiEvents.length === 0 ? (
            <div className="text-xs text-muted-foreground/60 italic">
              No wiki updates yet — the maintainer fires after extraction completes.
            </div>
          ) : (
            <ul className="space-y-1 max-h-44 overflow-y-auto text-xs">
              {wikiEvents.map((evt, idx) => {
                const failed = evt.event_type === "parse_failure";
                return (
                  <li
                    key={`${evt.ts}-${idx}`}
                    className={cn(
                      "border-l-2 pl-2 py-0.5",
                      failed ? "border-rose-400/60" : "border-emerald-500/40",
                    )}
                  >
                    <div className="flex items-center gap-1.5">
                      {failed ? (
                        <AlertCircle size={11} className="shrink-0 text-rose-500" />
                      ) : (
                        <FileText size={11} className="shrink-0 text-emerald-500" />
                      )}
                      <span className="text-foreground/80 truncate">
                        {String(evt.payload?.page_title ?? evt.payload?.page_id ?? evt.label)}
                      </span>
                    </div>
                    <div className="text-[10px] text-muted-foreground/70 ml-4">
                      {failed
                        ? "Parse failure"
                        : `${Number(evt.payload?.facts_integrated ?? 0)} fact${
                            Number(evt.payload?.facts_integrated) === 1 ? "" : "s"
                          } integrated`}
                      {" · "}
                      {fmtRelative(evt.ts)}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>

      {/* Stats footer */}
      <div className="flex items-center justify-between gap-3 border-t border-border bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground">
        <span>
          Throughput: <span className="font-medium text-foreground">{throughput}</span> msg/min
        </span>
        <span>
          Parse failures (10 min):{" "}
          <span
            className={cn(
              "font-medium",
              showParseFailureBanner ? "text-amber-600 dark:text-amber-400" : "text-foreground",
            )}
          >
            {parseFailureState?.count_last_10_min ?? 0}
          </span>
        </span>
      </div>
    </div>
  );
}

export default SyncMonitor;
