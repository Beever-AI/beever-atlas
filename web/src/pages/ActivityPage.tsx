import { useState } from "react";
import {
  CheckCircle2,
  XCircle,
  ChevronDown,
  Brain,
  Users,
  GitBranch,
  Clock,
  MessageSquare,
  Layers,
  Activity,
  Filter,
} from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { useSyncHistory } from "@/hooks/useStats";
import type { SyncHistoryEvent, BatchBreakdown } from "@/hooks/useStats";

function formatRelativeTime(timestamp: string): string {
  const diff = Date.now() - new Date(timestamp).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(timestamp).toLocaleDateString();
}

function formatFullTime(timestamp: string): string {
  return new Date(timestamp).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
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
    <div className="rounded-lg border border-border/40 bg-background/50 p-4">
      <div className="flex items-center justify-between mb-3">
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
        <div className="mb-3">
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground/60 font-semibold">
            Sample Facts
          </span>
          <ul className="mt-1.5 space-y-1.5">
            {breakdown.sample_facts.map((fact, i) => (
              <li
                key={i}
                className="text-xs text-foreground/70 leading-relaxed pl-3 border-l-2 border-violet-500/20"
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
  const hasBatches = batches.length > 0;
  const d = event.details;
  const totalFacts = (d.total_facts as number) ?? 0;
  const totalEntities = (d.total_entities as number) ?? 0;
  const totalRels = (d.total_relationships as number) ?? 0;
  const totalMessages = (d.total_messages as number) ?? 0;
  const isSuccess = event.event_type === "sync_completed";
  const channelName = (d.channel_name as string) ?? event.channel_id;
  const totalDuration = batches.reduce((sum, b) => sum + b.duration_seconds, 0);

  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden transition-shadow hover:shadow-sm">
      {/* Header */}
      <div
        className={`flex items-start gap-3 p-4 ${hasBatches ? "cursor-pointer" : ""}`}
        onClick={() => hasBatches && setExpanded(!expanded)}
      >
        <div
          className={`flex h-9 w-9 items-center justify-center rounded-full shrink-0 ${
            isSuccess ? "bg-emerald-500/10" : "bg-red-500/10"
          }`}
        >
          {isSuccess ? (
            <CheckCircle2 size={16} className="text-emerald-500" />
          ) : (
            <XCircle size={16} className="text-red-500" />
          )}
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-foreground">
              #{channelName}
            </h3>
            <span
              className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
                isSuccess
                  ? "bg-emerald-500/10 text-emerald-500"
                  : "bg-red-500/10 text-red-500"
              }`}
            >
              {isSuccess ? "Completed" : "Failed"}
            </span>
          </div>

          <div className="flex items-center gap-1.5 mt-1 text-xs text-muted-foreground">
            <span>{formatFullTime(event.timestamp)}</span>
            <span className="text-muted-foreground/30">&middot;</span>
            <span>{formatRelativeTime(event.timestamp)}</span>
            {totalDuration > 0 && (
              <>
                <span className="text-muted-foreground/30">&middot;</span>
                <span className="flex items-center gap-0.5">
                  <Clock size={10} />
                  {formatDuration(totalDuration)}
                </span>
              </>
            )}
          </div>

          {/* Stats row */}
          <div className="flex flex-wrap items-center gap-3 mt-2.5">
            {totalMessages > 0 && (
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <MessageSquare size={12} />
                {totalMessages} msgs
              </span>
            )}
            {totalFacts > 0 && (
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <Brain size={12} className="text-violet-400" />
                {totalFacts} facts
              </span>
            )}
            {totalEntities > 0 && (
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <Users size={12} className="text-blue-400" />
                {totalEntities} entities
              </span>
            )}
            {totalRels > 0 && (
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <GitBranch size={12} className="text-amber-400" />
                {totalRels} rels
              </span>
            )}
          </div>
        </div>

        {hasBatches && (
          <div
            className={`mt-2 text-muted-foreground/40 transition-transform duration-200 ${
              expanded ? "rotate-180" : ""
            }`}
          >
            <ChevronDown size={16} />
          </div>
        )}
      </div>

      {/* Expandable batch details */}
      {expanded && batches.length > 0 && (
        <div className="border-t border-border/50 bg-muted/20 p-4">
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground/50 font-medium uppercase tracking-widest mb-3">
            <Layers size={11} />
            {batches.length} batch{batches.length !== 1 ? "es" : ""}
          </div>
          <div className="space-y-3">
            {batches.map((batch) => (
              <BatchDetail key={batch.batch_num} breakdown={batch} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function ActivityPage() {
  const { entries, loading } = useSyncHistory(50);
  const [filter, setFilter] = useState<"all" | "completed" | "failed">("all");

  const filtered = entries.filter((e) => {
    if (filter === "completed") return e.event_type === "sync_completed";
    if (filter === "failed") return e.event_type === "sync_failed";
    return true;
  });

  const completedCount = entries.filter((e) => e.event_type === "sync_completed").length;
  const failedCount = entries.filter((e) => e.event_type === "sync_failed").length;

  // Aggregate stats across all visible entries
  const totalFacts = filtered.reduce((s, e) => s + ((e.details.total_facts as number) ?? 0), 0);
  const totalEntities = filtered.reduce((s, e) => s + ((e.details.total_entities as number) ?? 0), 0);
  const totalRels = filtered.reduce((s, e) => s + ((e.details.total_relationships as number) ?? 0), 0);

  return (
    <div className="min-h-full">
      <div className="max-w-[960px] mx-auto p-6 sm:p-8 lg:p-12">
        {/* Page header */}
        <div className="flex items-center gap-3 mb-2">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary/10 text-primary">
            <Activity size={20} />
          </div>
          <div>
            <h1 className="font-heading text-2xl tracking-tight text-foreground">
              Sync History
            </h1>
            <p className="text-sm text-muted-foreground">
              Extraction results from every channel sync
            </p>
          </div>
        </div>

        {/* Summary cards */}
        {!loading && entries.length > 0 && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-6 mb-6">
            <div className="rounded-xl border border-border bg-card p-3.5">
              <p className="text-2xl font-semibold tabular-nums text-foreground">
                {entries.length}
              </p>
              <p className="text-xs text-muted-foreground mt-0.5">Total Syncs</p>
            </div>
            <div className="rounded-xl border border-border bg-card p-3.5">
              <p className="text-2xl font-semibold tabular-nums text-violet-400">
                {totalFacts.toLocaleString()}
              </p>
              <p className="text-xs text-muted-foreground mt-0.5">Facts Extracted</p>
            </div>
            <div className="rounded-xl border border-border bg-card p-3.5">
              <p className="text-2xl font-semibold tabular-nums text-blue-400">
                {totalEntities.toLocaleString()}
              </p>
              <p className="text-xs text-muted-foreground mt-0.5">Entities Found</p>
            </div>
            <div className="rounded-xl border border-border bg-card p-3.5">
              <p className="text-2xl font-semibold tabular-nums text-amber-400">
                {totalRels.toLocaleString()}
              </p>
              <p className="text-xs text-muted-foreground mt-0.5">Relationships</p>
            </div>
          </div>
        )}

        {/* Filter tabs */}
        <div className="flex items-center gap-1 mb-5">
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

        {/* Sync history list */}
        {loading ? (
          <div className="space-y-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="rounded-xl border border-border bg-card p-4">
                <div className="flex items-start gap-3">
                  <Skeleton className="h-9 w-9 rounded-full shrink-0" />
                  <div className="flex-1 space-y-2">
                    <Skeleton className="h-4 w-40" />
                    <Skeleton className="h-3 w-56" />
                    <div className="flex gap-4">
                      <Skeleton className="h-3 w-20" />
                      <Skeleton className="h-3 w-20" />
                      <Skeleton className="h-3 w-20" />
                    </div>
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
              {filter === "all" ? "No sync history yet" : `No ${filter} syncs`}
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              {filter === "all"
                ? "Sync a channel to see extraction results here."
                : "Try changing the filter to see other results."}
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {filtered.map((event) => (
              <SyncCard key={event.id} event={event} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
