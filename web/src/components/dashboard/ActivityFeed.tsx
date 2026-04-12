import { useMemo, useState } from "react";
import {
  HelpCircle,
  ChevronDown,
  Clock,
  Layers,
  MessageSquare,
} from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import type { ActivityEvent, SyncHistoryEvent, BatchBreakdown } from "@/hooks/useStats";

function formatRelativeTime(timestamp: string): string {
  const diff = Date.now() - new Date(timestamp).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
}

function isToday(ts: string): boolean {
  const d = new Date(ts);
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}

function isSyncEvent(event: ActivityEvent): event is SyncHistoryEvent {
  return event.event_type === "sync_completed" || event.event_type === "sync_failed";
}

function humanTitle(event: ActivityEvent): { text: string; tone: "success" | "error" | "muted" } {
  const d = event.details;
  switch (event.event_type) {
    case "sync_completed":
    case "sync_complete": {
      const channel = (d.channel_name as string) ?? event.channel_id;
      return { text: `Synced #${channel}`, tone: "success" };
    }
    case "sync_failed": {
      const channel = (d.channel_name as string) ?? event.channel_id;
      return { text: `Couldn't sync #${channel}`, tone: "error" };
    }
    case "new_entity": {
      const name = (d.entity_name as string) ?? "Unknown";
      return { text: `Learned about ${name}`, tone: "muted" };
    }
    case "consolidation_completed":
    case "consolidation_complete":
      return { text: "Memory organized", tone: "muted" };
    default:
      return { text: event.event_type.replace(/_/g, " "), tone: "muted" };
  }
}

function StatusDot({ tone }: { tone: "success" | "error" | "muted" }) {
  const color =
    tone === "success"
      ? "bg-emerald-500"
      : tone === "error"
      ? "bg-red-500"
      : "bg-muted-foreground/40";
  return <span className={`mt-1.5 h-2 w-2 rounded-full shrink-0 ${color}`} aria-hidden />;
}

function BatchDetail({ breakdown }: { breakdown: BatchBreakdown }) {
  return (
    <div className="rounded-lg border border-border/40 bg-background/50 p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-semibold text-foreground tracking-tight">
          Batch {breakdown.batch_num}
        </span>
        <span className="flex items-center gap-1 text-[11px] text-muted-foreground tabular-nums">
          <Clock size={10} />
          {formatDuration(breakdown.duration_seconds)}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground tabular-nums">
        <span>{breakdown.facts_count} insights</span>
        <span className="text-muted-foreground/30">·</span>
        <span>{breakdown.entities_count} people &amp; topics</span>
        <span className="text-muted-foreground/30">·</span>
        <span>{breakdown.relationships_count} connections</span>
      </div>

      {breakdown.sample_facts.length > 0 && (
        <div className="mt-2.5 mb-2.5">
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground/60 font-semibold">
            Sample insights
          </span>
          <ul className="mt-1.5 space-y-1">
            {breakdown.sample_facts.map((fact, i) => (
              <li
                key={i}
                className="text-xs text-foreground/70 leading-relaxed pl-2.5 border-l-2 border-border"
              >
                {fact}
              </li>
            ))}
          </ul>
        </div>
      )}

      {breakdown.sample_entities.length > 0 && (
        <div className="mb-2.5">
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
          <ul className="mt-1.5 space-y-0.5">
            {breakdown.sample_relationships.map((r, i) => (
              <li key={i} className="text-xs text-foreground/70 flex items-center gap-1">
                <span className="font-medium">{r.source}</span>
                <span className="text-muted-foreground/40 text-[10px]">{r.type}</span>
                <span className="text-muted-foreground/30">→</span>
                <span className="font-medium">{r.target}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function SyncHistoryRow({ event }: { event: SyncHistoryEvent }) {
  const [expanded, setExpanded] = useState(false);
  const batches = event.details.results_summary ?? [];
  const hasBatches = batches.length > 0;
  const d = event.details;
  const totalFacts = (d.total_facts as number) ?? 0;
  const totalEntities = (d.total_entities as number) ?? 0;
  const totalRels = (d.total_relationships as number) ?? 0;
  const totalMessages = (d.total_messages as number) ?? 0;
  const isSuccess = event.event_type === "sync_completed";
  const errorMessage = (d.error as string) ?? null;
  const title = humanTitle(event);

  const metrics: string[] = [];
  if (isSuccess) {
    if (totalMessages > 0) metrics.push(`${totalMessages} msgs`);
    if (totalFacts > 0) metrics.push(`${totalFacts} insights`);
    if (totalEntities > 0) metrics.push(`${totalEntities} people & topics`);
    if (totalRels > 0) metrics.push(`${totalRels} connections`);
  }

  return (
    <div className="group">
      <div
        className={`flex items-start gap-3 px-4 py-3.5 transition-colors ${
          hasBatches ? "cursor-pointer hover:bg-muted/30" : ""
        }`}
        onClick={() => hasBatches && setExpanded(!expanded)}
      >
        <StatusDot tone={title.tone} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <p className="text-sm font-medium text-foreground leading-snug">
              {title.text}
            </p>
            {!isSuccess && !errorMessage && (
              <HelpCircle
                size={12}
                className="text-muted-foreground/50 shrink-0"
                aria-label="Reason unavailable — open details for more"
              />
            )}
            <span className="text-[11px] text-muted-foreground/60 tabular-nums shrink-0">
              {formatRelativeTime(event.timestamp)}
            </span>
          </div>

          {!isSuccess && errorMessage && (
            <p className="mt-1 text-xs text-red-600 dark:text-red-400 leading-relaxed">
              {errorMessage}
            </p>
          )}

          {metrics.length > 0 && (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mt-1.5 text-xs text-muted-foreground tabular-nums">
              {metrics.map((item, i) => (
                <span key={i} className="inline-flex items-center">
                  {i > 0 && <span className="mr-2 text-muted-foreground/30">·</span>}
                  {item}
                </span>
              ))}
            </div>
          )}
        </div>

        {hasBatches && (
          <div
            className={`mt-1 text-muted-foreground/40 transition-transform duration-200 ${
              expanded ? "rotate-180" : ""
            }`}
          >
            <ChevronDown size={14} />
          </div>
        )}
      </div>

      {expanded && batches.length > 0 && (
        <div className="px-4 pb-3">
          <div className="ml-5 space-y-2">
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground/50 font-medium uppercase tracking-widest mb-1">
              <Layers size={11} />
              {batches.length} batch{batches.length !== 1 ? "es" : ""}
            </div>
            {batches.map((batch) => (
              <BatchDetail key={batch.batch_num} breakdown={batch} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function SimpleRow({ event }: { event: ActivityEvent }) {
  const title = humanTitle(event);
  return (
    <div className="flex items-start gap-3 px-4 py-3">
      <StatusDot tone={title.tone} />
      <div className="flex-1 min-w-0 flex items-center gap-2">
        <p className="text-sm text-foreground/80 leading-snug">{title.text}</p>
        <span className="text-[11px] text-muted-foreground/60 tabular-nums shrink-0">
          {formatRelativeTime(event.timestamp)}
        </span>
      </div>
    </div>
  );
}

interface ActivityFeedProps {
  events: ActivityEvent[];
  loading: boolean;
}

export function ActivityFeed({ events, loading }: ActivityFeedProps) {
  // Dedupe consecutive background events of the same type into a single muted line
  const processed = useMemo(() => {
    const result: (
      | { kind: "event"; event: ActivityEvent }
      | { kind: "group"; label: string; count: number; latestTs: string }
    )[] = [];
    let i = 0;
    while (i < events.length) {
      const e = events[i];
      const isBackground =
        e.event_type === "consolidation_completed" ||
        e.event_type === "consolidation_complete";
      if (isBackground) {
        let j = i;
        while (
          j < events.length &&
          (events[j].event_type === "consolidation_completed" ||
            events[j].event_type === "consolidation_complete")
        ) {
          j++;
        }
        const count = j - i;
        result.push({
          kind: "group",
          label: `Memory organized${count > 1 ? ` · ${count}×` : ""}`,
          count,
          latestTs: e.timestamp,
        });
        i = j;
      } else {
        result.push({ kind: "event", event: e });
        i++;
      }
    }
    return result;
  }, [events]);

  // Split into Today / Earlier
  const { today, earlier } = useMemo(() => {
    const today: typeof processed = [];
    const earlier: typeof processed = [];
    for (const item of processed) {
      const ts = item.kind === "event" ? item.event.timestamp : item.latestTs;
      if (isToday(ts)) today.push(item);
      else earlier.push(item);
    }
    return { today, earlier };
  }, [processed]);

  const renderItem = (
    item:
      | { kind: "event"; event: ActivityEvent }
      | { kind: "group"; label: string; count: number; latestTs: string },
    idx: number,
  ) => {
    if (item.kind === "group") {
      return (
        <div key={`group-${idx}`} className="flex items-center gap-3 px-4 py-2">
          <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/30 shrink-0" aria-hidden />
          <p className="text-xs text-muted-foreground/70 flex-1">{item.label}</p>
          <span className="text-[11px] text-muted-foreground/50 tabular-nums shrink-0">
            {formatRelativeTime(item.latestTs)}
          </span>
        </div>
      );
    }
    const { event } = item;
    return isSyncEvent(event) ? (
      <SyncHistoryRow key={event.id} event={event} />
    ) : (
      <SimpleRow key={event.id} event={event} />
    );
  };

  const SectionHeader = ({ label }: { label: string }) => (
    <div className="flex items-center gap-2 px-4 pt-3 pb-1.5">
      <span className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/60">
        {label}
      </span>
      <div className="flex-1 h-px bg-border/40" />
    </div>
  );

  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <h2 className="font-heading text-base font-medium text-foreground">
          Recent Activity
        </h2>
        {events.length > 0 && (
          <span className="text-xs text-muted-foreground/50 tabular-nums">
            Last {events.length} {events.length === 1 ? "update" : "updates"}
          </span>
        )}
      </div>

      <div className="max-h-[520px] overflow-y-auto">
        {loading ? (
          <div className="divide-y divide-border/50">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="flex items-start gap-3 px-4 py-3.5">
                <Skeleton className="h-2 w-2 rounded-full mt-1.5 shrink-0" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-3.5 w-2/3" />
                  <Skeleton className="h-3 w-3/4" />
                </div>
              </div>
            ))}
          </div>
        ) : events.length === 0 ? (
          <div className="px-4 py-12 text-center">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted mx-auto mb-3">
              <MessageSquare size={18} className="text-muted-foreground/50" />
            </div>
            <p className="text-sm font-medium text-foreground/70">No activity yet</p>
            <p className="text-xs text-muted-foreground mt-1">
              Connect a channel to start learning.
            </p>
          </div>
        ) : (
          <div>
            {today.length > 0 && (
              <>
                <SectionHeader label="Today" />
                <div className="divide-y divide-border/50">{today.map(renderItem)}</div>
              </>
            )}
            {earlier.length > 0 && (
              <>
                {today.length > 0 && <SectionHeader label="Earlier" />}
                <div className="divide-y divide-border/50">{earlier.map(renderItem)}</div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
