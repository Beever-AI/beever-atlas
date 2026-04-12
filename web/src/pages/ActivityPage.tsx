import { useMemo, useState } from "react";
import {
  HelpCircle,
  ChevronDown,
  Clock,
  Layers,
  Activity,
  Filter,
} from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { useSyncHistory } from "@/hooks/useStats";
import type { SyncHistoryEvent, BatchBreakdown } from "@/hooks/useStats";

function formatExactTime(timestamp: string): string {
  return new Date(timestamp).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
}

function startOfDay(ts: string): number {
  const d = new Date(ts);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function dayLabel(dayTs: number): string {
  const today = startOfDay(new Date().toISOString());
  const yesterday = today - 86_400_000;
  const d = new Date(dayTs);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  const dateStr = d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: sameYear ? undefined : "numeric",
  });
  if (dayTs === today) return `Today · ${dateStr}`;
  if (dayTs === yesterday) return `Yesterday · ${dateStr}`;
  const diffDays = Math.floor((today - dayTs) / 86_400_000);
  if (diffDays < 7) {
    const weekday = d.toLocaleDateString(undefined, { weekday: "long" });
    return `${weekday} · ${dateStr}`;
  }
  return dateStr;
}

function MetaSeparator() {
  return <span className="text-muted-foreground/30">·</span>;
}

function MetricLine({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mt-2 text-xs text-muted-foreground tabular-nums">
      {items.map((item, i) => (
        <span key={i} className="inline-flex items-center">
          {i > 0 && <span className="mr-2 text-muted-foreground/30">·</span>}
          {item}
        </span>
      ))}
    </div>
  );
}

function BatchDetail({ breakdown }: { breakdown: BatchBreakdown }) {
  const isFailed = !!breakdown.error;

  return (
    <div className={`rounded-lg border bg-background/50 p-4 ${isFailed ? "border-red-500/30" : "border-border/40"}`}>
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-semibold text-foreground tracking-tight">
          Batch {breakdown.batch_num}
        </span>
        {breakdown.duration_seconds > 0 && (
          <span className="flex items-center gap-1 text-[11px] text-muted-foreground tabular-nums">
            <Clock size={10} />
            {formatDuration(breakdown.duration_seconds)}
          </span>
        )}
      </div>

      {isFailed && (
        <div className="rounded-md border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/20 px-3 py-2 mb-3">
          <div className="text-[11px] text-red-700 dark:text-red-300">{breakdown.error}</div>
        </div>
      )}

      {!isFailed && (
        <MetricLine
          items={[
            `${breakdown.facts_count} insights`,
            `${breakdown.entities_count} people & topics`,
            `${breakdown.relationships_count} connections`,
          ]}
        />
      )}

      {breakdown.sample_facts.length > 0 && (
        <div className="mt-3 mb-3">
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground/60 font-semibold">
            Sample insights
          </span>
          <ul className="mt-1.5 space-y-1.5">
            {breakdown.sample_facts.map((fact, i) => (
              <li
                key={i}
                className="text-xs text-foreground/70 leading-relaxed pl-3 border-l-2 border-border"
              >
                {fact}
              </li>
            ))}
          </ul>
        </div>
      )}

      {breakdown.sample_entities.length > 0 && (
        <div className="mb-3">
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground/60 font-semibold">
            People &amp; topics
          </span>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {breakdown.sample_entities.map((e, i) => (
              <span
                key={i}
                className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] bg-muted/80 border border-border/40 text-foreground/80"
              >
                {e.name}
                <span className="text-muted-foreground/50 text-[9px] uppercase">{e.type}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {breakdown.sample_relationships.length > 0 && (
        <div>
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground/60 font-semibold">
            Connections
          </span>
          <ul className="mt-1.5 space-y-1">
            {breakdown.sample_relationships.map((r, i) => (
              <li key={i} className="text-xs text-foreground/70 flex items-center gap-1">
                <span className="font-medium">{r.source}</span>
                <span className="text-muted-foreground/40 text-[10px]">{r.type}</span>
                <span className="text-muted-foreground/30">&rarr;</span>
                <span className="font-medium">{r.target}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function SyncCard({ event }: { event: SyncHistoryEvent }) {
  const [expanded, setExpanded] = useState(false);
  const batches = event.details.results_summary ?? [];
  const d = event.details;
  const totalFacts = (d.total_facts as number) ?? 0;
  const totalEntities = (d.total_entities as number) ?? 0;
  const totalRels = (d.total_relationships as number) ?? 0;
  const totalMessages = (d.total_messages as number) ?? 0;
  const isSuccess = event.event_type === "sync_completed";
  const channelName = (d.channel_name as string) ?? event.channel_id;
  const errorMessage = (d.error as string) ?? null;
  const totalDuration = batches.reduce((sum, b) => sum + b.duration_seconds, 0);
  const hasExpandable = batches.length > 0 || (!isSuccess && errorMessage);

  const title = isSuccess
    ? `Synced #${channelName}`
    : `Couldn't sync #${channelName}`;

  const metrics: string[] = [];
  if (isSuccess) {
    if (totalMessages > 0) metrics.push(`${totalMessages} msgs`);
    if (totalFacts > 0) metrics.push(`${totalFacts} insights`);
    if (totalEntities > 0) metrics.push(`${totalEntities} people & topics`);
    if (totalRels > 0) metrics.push(`${totalRels} connections`);
  }

  return (
    <div
      className={`rounded-xl border bg-card overflow-hidden transition-shadow hover:shadow-sm ${
        isSuccess ? "border-border" : "border-red-500/30"
      }`}
    >
      <div
        className={`flex items-start gap-3 p-4 ${hasExpandable ? "cursor-pointer" : ""}`}
        onClick={() => hasExpandable && setExpanded(!expanded)}
      >
        <div
          className={`mt-1.5 h-2 w-2 rounded-full shrink-0 ${
            isSuccess ? "bg-emerald-500" : "bg-red-500"
          }`}
          aria-hidden
        />

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5">
            <h3 className="text-sm font-semibold text-foreground">{title}</h3>
            {!isSuccess && !errorMessage && (
              <HelpCircle
                size={13}
                className="text-muted-foreground/50 shrink-0"
                aria-label="Reason unavailable — open details for more"
              />
            )}
          </div>

          <div className="flex items-center gap-2 mt-1 text-xs text-muted-foreground tabular-nums">
            <span>{formatExactTime(event.timestamp)}</span>
            {totalDuration > 0 && (
              <>
                <MetaSeparator />
                <span className="flex items-center gap-1">
                  <Clock size={10} />
                  {formatDuration(totalDuration)}
                </span>
              </>
            )}
          </div>

          {!isSuccess && errorMessage && (
            <p className="mt-2 text-xs text-red-600 dark:text-red-400 leading-relaxed">
              {errorMessage}
            </p>
          )}

          <MetricLine items={metrics} />
        </div>

        {hasExpandable && (
          <div
            className={`mt-1 text-muted-foreground/40 transition-transform duration-200 ${
              expanded ? "rotate-180" : ""
            }`}
          >
            <ChevronDown size={16} />
          </div>
        )}
      </div>

      {expanded && hasExpandable && (
        <div className="border-t border-border/50 bg-muted/20 p-4">
          {!isSuccess && errorMessage && batches.length === 0 && (
            <div className="rounded-md border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/20 px-3 py-2 mb-3">
              <div className="text-[11px] text-red-700 dark:text-red-300">{errorMessage}</div>
            </div>
          )}
          {batches.length > 0 && (
            <>
              <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground/50 font-medium uppercase tracking-widest mb-3">
                <Layers size={11} />
                {batches.length} batch{batches.length !== 1 ? "es" : ""}
              </div>
              <div className="space-y-3">
                {batches.map((batch) => (
                  <BatchDetail key={batch.batch_num} breakdown={batch} />
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

type TimeRange = "all" | "today" | "7d" | "30d";

const rangeMs: Record<TimeRange, number | null> = {
  all: null,
  today: 0,
  "7d": 7 * 86_400_000,
  "30d": 30 * 86_400_000,
};

export function ActivityPage() {
  const { entries, loading } = useSyncHistory(50);
  const [filter, setFilter] = useState<"all" | "completed" | "failed">("all");
  const [range, setRange] = useState<TimeRange>("all");

  const rangeCutoff = useMemo(() => {
    if (range === "all") return null;
    if (range === "today") return startOfDay(new Date().toISOString());
    return Date.now() - (rangeMs[range] as number);
  }, [range]);

  const filtered = entries.filter((e) => {
    if (filter === "completed" && e.event_type !== "sync_completed") return false;
    if (filter === "failed" && e.event_type !== "sync_failed") return false;
    if (rangeCutoff !== null && new Date(e.timestamp).getTime() < rangeCutoff) return false;
    return true;
  });

  const completedCount = entries.filter((e) => e.event_type === "sync_completed").length;
  const failedCount = entries.filter((e) => e.event_type === "sync_failed").length;

  // Aggregate totals across completed syncs (humanized summary)
  const completedEntries = entries.filter((e) => e.event_type === "sync_completed");
  const summaryInsights = completedEntries.reduce(
    (s, e) => s + ((e.details.total_facts as number) ?? 0),
    0,
  );
  const summaryChannels = new Set(
    completedEntries.map((e) => (e.details.channel_name as string) ?? e.channel_id),
  ).size;

  // Group by day
  const dayGroups = useMemo(() => {
    const groups: { dayTs: number; label: string; items: SyncHistoryEvent[] }[] = [];
    const byDay = new Map<number, SyncHistoryEvent[]>();
    for (const e of filtered) {
      const day = startOfDay(e.timestamp);
      const bucket = byDay.get(day) ?? [];
      bucket.push(e);
      byDay.set(day, bucket);
    }
    const sortedDays = [...byDay.keys()].sort((a, b) => b - a);
    for (const dayTs of sortedDays) {
      groups.push({ dayTs, label: dayLabel(dayTs), items: byDay.get(dayTs)! });
    }
    return groups;
  }, [filtered]);

  return (
    <div className="h-full overflow-auto">
      <div className="max-w-[960px] mx-auto p-6 sm:p-8 lg:p-12">
        {/* Page header */}
        <div className="flex items-center gap-3 mb-2">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary/10 text-primary">
            <Activity size={20} />
          </div>
          <div>
            <h1 className="font-heading text-2xl tracking-tight text-foreground">
              Activity
            </h1>
            <p className="text-sm text-muted-foreground">
              What Beever has learned from your channels
            </p>
          </div>
        </div>

        {/* Human summary */}
        {!loading && entries.length > 0 && (
          <div className="mt-6 mb-6 rounded-xl border border-border bg-card px-5 py-4">
            <p className="text-sm text-foreground/80 leading-relaxed">
              Beever captured{" "}
              <span className="font-semibold text-foreground tabular-nums">
                {summaryInsights.toLocaleString()}
              </span>{" "}
              {summaryInsights === 1 ? "insight" : "insights"} across{" "}
              <span className="font-semibold text-foreground tabular-nums">
                {summaryChannels}
              </span>{" "}
              {summaryChannels === 1 ? "channel" : "channels"} across your last{" "}
              <span className="font-semibold text-foreground tabular-nums">
                {entries.length}
              </span>{" "}
              {entries.length === 1 ? "sync" : "syncs"}
              {failedCount > 0 && (
                <>
                  {" "}
                  —{" "}
                  <button
                    type="button"
                    onClick={() => setFilter("failed")}
                    className="text-red-500 hover:text-red-600 underline decoration-dotted underline-offset-2 transition-colors"
                  >
                    {failedCount} failed
                  </button>
                </>
              )}
              .
            </p>
          </div>
        )}

        {/* Filter tabs */}
        <div className="flex items-center flex-wrap gap-x-4 gap-y-2 mb-5">
          <div className="flex items-center gap-1">
            <Filter size={13} className="text-muted-foreground/40 mr-1" />
            {(
              [
                { key: "all", label: "All", count: entries.length },
                { key: "completed", label: "Completed", count: completedCount },
                { key: "failed", label: "Failed", count: failedCount },
              ] as const
            ).map(({ key, label, count }) => (
              <button
                key={key}
                onClick={() => setFilter(key)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                  filter === key
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted"
                }`}
              >
                {label}
                {count > 0 && (
                  <span className="ml-1.5 tabular-nums text-current/60">{count}</span>
                )}
              </button>
            ))}
          </div>

          <div className="h-5 w-px bg-border/60 hidden sm:block" />

          <div className="flex items-center gap-1">
            {(
              [
                { key: "all", label: "All time" },
                { key: "today", label: "Today" },
                { key: "7d", label: "7 days" },
                { key: "30d", label: "30 days" },
              ] as const
            ).map(({ key, label }) => (
              <button
                key={key}
                onClick={() => setRange(key)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                  range === key
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {/* Sync history list */}
        {loading ? (
          <div className="space-y-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="rounded-xl border border-border bg-card p-4">
                <div className="flex items-start gap-3">
                  <Skeleton className="h-2 w-2 rounded-full mt-1.5 shrink-0" />
                  <div className="flex-1 space-y-2">
                    <Skeleton className="h-4 w-40" />
                    <Skeleton className="h-3 w-56" />
                    <Skeleton className="h-3 w-64" />
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-card p-12 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted mx-auto mb-3">
              <Activity size={22} className="text-muted-foreground/40" />
            </div>
            <p className="text-sm font-medium text-foreground/70">
              {filter === "all" ? "No activity yet" : `No ${filter} syncs`}
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              {filter === "all"
                ? "Connect a channel to start learning — sync results will appear here."
                : "Try changing the filter to see other results."}
            </p>
          </div>
        ) : (
          <div className="space-y-6">
            {dayGroups.map((group) => (
              <section key={group.dayTs}>
                <div className="flex items-center gap-3 mb-2.5 px-1">
                  <h2 className="text-[11px] font-semibold uppercase tracking-widest text-muted-foreground/70">
                    {group.label}
                  </h2>
                  <div className="flex-1 h-px bg-border/60" />
                  <span className="text-[11px] text-muted-foreground/50 tabular-nums">
                    {group.items.length}
                  </span>
                </div>
                <div className="space-y-2.5">
                  {group.items.map((event) => (
                    <SyncCard key={event.id} event={event} />
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
