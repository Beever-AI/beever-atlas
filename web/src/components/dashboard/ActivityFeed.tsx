import { useState } from "react";
import {
  CheckCircle2,
  XCircle,
  UserPlus,
  ChevronDown,
  Brain,
  Users,
  GitBranch,
  Clock,
  MessageSquare,
  Layers,
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

function eventIcon(eventType: string) {
  switch (eventType) {
    case "sync_completed":
    case "sync_complete":
      return (
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-emerald-500/10">
          <CheckCircle2 size={15} className="text-emerald-500" />
        </div>
      );
    case "sync_failed":
      return (
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-red-500/10">
          <XCircle size={15} className="text-red-500" />
        </div>
      );
    case "new_entity":
      return (
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-blue-500/10">
          <UserPlus size={15} className="text-blue-500" />
        </div>
      );
    default:
      return (
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-muted">
          <CheckCircle2 size={15} className="text-muted-foreground" />
        </div>
      );
  }
}

function isSyncEvent(event: ActivityEvent): event is SyncHistoryEvent {
  return event.event_type === "sync_completed" || event.event_type === "sync_failed";
}

function eventDescription(event: ActivityEvent): string {
  const d = event.details;
  switch (event.event_type) {
    case "sync_completed":
    case "sync_complete": {
      const channel = (d.channel_name as string) ?? event.channel_id;
      return `Synced #${channel}`;
    }
    case "sync_failed": {
      const channel = (d.channel_name as string) ?? event.channel_id;
      return `Sync failed for #${channel}`;
    }
    case "new_entity": {
      const name = (d.entity_name as string) ?? "Unknown";
      const type = (d.entity_type as string) ?? "entity";
      return `New entity: ${name} (${type})`;
    }
    default:
      return event.event_type;
  }
}

function StatPill({
  icon,
  value,
  label,
  color,
}: {
  icon: React.ReactNode;
  value: number;
  label: string;
  color: string;
}) {
  return (
    <div className={`inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium ${color}`}>
      {icon}
      <span className="tabular-nums">{value}</span>
      <span className="text-current/60">{label}</span>
    </div>
  );
}

function BatchDetail({ breakdown }: { breakdown: BatchBreakdown }) {
  return (
    <div className="rounded-lg border border-border/40 bg-background/50 p-3">
      <div className="flex items-center justify-between mb-2.5">
        <span className="text-xs font-semibold text-foreground tracking-tight">
          Batch {breakdown.batch_num}
        </span>
        <span className="flex items-center gap-1 text-[11px] text-muted-foreground tabular-nums">
          <Clock size={10} />
          {formatDuration(breakdown.duration_seconds)}
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5 mb-3">
        <StatPill
          icon={<Brain size={11} />}
          value={breakdown.facts_count}
          label="facts"
          color="bg-violet-500/10 text-violet-400"
        />
        <StatPill
          icon={<Users size={11} />}
          value={breakdown.entities_count}
          label="entities"
          color="bg-blue-500/10 text-blue-400"
        />
        <StatPill
          icon={<GitBranch size={11} />}
          value={breakdown.relationships_count}
          label="rels"
          color="bg-amber-500/10 text-amber-400"
        />
      </div>

      {breakdown.sample_facts.length > 0 && (
        <div className="mb-2.5">
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground/60 font-semibold">
            Sample Facts
          </span>
          <ul className="mt-1.5 space-y-1">
            {breakdown.sample_facts.map((fact, i) => (
              <li
                key={i}
                className="text-xs text-foreground/70 leading-relaxed pl-2.5 border-l-2 border-violet-500/20"
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
            Entities
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
            Relationships
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

  return (
    <div className="group">
      <div
        className={`flex items-start gap-3 px-4 py-3.5 transition-colors ${
          hasBatches ? "cursor-pointer hover:bg-muted/30" : ""
        }`}
        onClick={() => hasBatches && setExpanded(!expanded)}
      >
        {eventIcon(event.event_type)}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <p className="text-sm font-medium text-foreground leading-snug">
              {eventDescription(event)}
            </p>
            <span className="text-[11px] text-muted-foreground/60 tabular-nums shrink-0">
              {formatRelativeTime(event.timestamp)}
            </span>
          </div>

          {/* Summary stats row */}
          <div className="flex flex-wrap items-center gap-3 mt-1.5 text-xs text-muted-foreground">
            {totalMessages > 0 && (
              <span className="flex items-center gap-1">
                <MessageSquare size={11} />
                {totalMessages} messages
              </span>
            )}
            {totalFacts > 0 && (
              <span className="flex items-center gap-1">
                <Brain size={11} className="text-violet-400" />
                {totalFacts} facts
              </span>
            )}
            {totalEntities > 0 && (
              <span className="flex items-center gap-1">
                <Users size={11} className="text-blue-400" />
                {totalEntities} entities
              </span>
            )}
            {totalRels > 0 && (
              <span className="flex items-center gap-1">
                <GitBranch size={11} className="text-amber-400" />
                {totalRels} relationships
              </span>
            )}
          </div>
        </div>

        {hasBatches && (
          <div
            className={`mt-1.5 text-muted-foreground/40 transition-transform duration-200 ${
              expanded ? "rotate-180" : ""
            }`}
          >
            <ChevronDown size={14} />
          </div>
        )}
      </div>

      {expanded && batches.length > 0 && (
        <div className="px-4 pb-3">
          <div className="ml-11 space-y-2">
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

interface ActivityFeedProps {
  events: ActivityEvent[];
  loading: boolean;
}

export function ActivityFeed({ events, loading }: ActivityFeedProps) {
  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <h2 className="font-heading text-base font-medium text-foreground">
          Recent Activity
        </h2>
        {events.length > 0 && (
          <span className="text-xs text-muted-foreground/50 tabular-nums">
            {events.length} events
          </span>
        )}
      </div>

      <div className="max-h-[520px] overflow-y-auto">
        {loading ? (
          <div className="divide-y divide-border">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="flex items-start gap-3 px-4 py-3.5">
                <Skeleton className="h-8 w-8 rounded-full shrink-0" />
                <div className="flex-1 space-y-2">
                  <Skeleton className="h-3.5 w-2/3" />
                  <div className="flex gap-3">
                    <Skeleton className="h-3 w-20" />
                    <Skeleton className="h-3 w-16" />
                    <Skeleton className="h-3 w-16" />
                  </div>
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
              Sync a channel to see extraction history here.
            </p>
          </div>
        ) : (
          <div className="divide-y divide-border/50">
            {events.map((event) =>
              isSyncEvent(event) ? (
                <SyncHistoryRow key={event.id} event={event} />
              ) : (
                <div key={event.id} className="flex items-start gap-3 px-4 py-3.5">
                  {eventIcon(event.event_type)}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <p className="text-sm text-foreground leading-snug">
                        {eventDescription(event)}
                      </p>
                      <span className="text-[11px] text-muted-foreground/60 tabular-nums shrink-0">
                        {formatRelativeTime(event.timestamp)}
                      </span>
                    </div>
                  </div>
                </div>
              )
            )}
          </div>
        )}
      </div>
    </div>
  );
}
