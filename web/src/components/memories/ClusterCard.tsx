import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { MemoryTier1, MemoryTier2 } from "@/lib/types";
import { FactCard } from "./FactCard";

interface ClusterCardProps {
  cluster: MemoryTier1;
  facts: MemoryTier2[];
}

export function ClusterCard({ cluster, facts }: ClusterCardProps) {
  const [expanded, setExpanded] = useState(false);

  const memberFacts = facts.filter((f) => f.cluster_id === cluster.id);

  return (
    <div className="rounded-2xl border border-border bg-card shadow-sm overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-start gap-3 p-4 sm:p-5 text-left hover:bg-muted/40 transition-colors"
      >
        {expanded ? (
          <ChevronDown size={16} className="text-primary shrink-0 mt-0.5" />
        ) : (
          <ChevronRight size={16} className="text-primary shrink-0 mt-0.5" />
        )}
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h4 className="text-xl font-semibold text-foreground leading-tight">
              {cluster.topic}
            </h4>
            <span className="text-sm text-muted-foreground shrink-0">
              {cluster.fact_count} facts
            </span>
          </div>
          <p className="text-sm text-muted-foreground mt-1 leading-relaxed">
            {cluster.summary}
          </p>
          <div className="mt-2 text-xs text-muted-foreground">
            {new Date(cluster.date_range.start).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
            })}{" "}
            -{" "}
            {new Date(cluster.date_range.end).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
            })}
          </div>
        </div>
        <div className="hidden lg:flex gap-1 shrink-0">
          {cluster.topic_tags.map((tag) => (
            <span
              key={tag}
              className="px-2 py-0.5 text-xs rounded-full bg-muted text-muted-foreground"
            >
              {tag}
            </span>
          ))}
        </div>
      </button>

      {expanded && (
        <div className="border-t border-border bg-muted/25 p-3 sm:p-4 space-y-2">
          {memberFacts.length > 0 ? (
            memberFacts.map((fact) => <FactCard key={fact.id} fact={fact} />)
          ) : (
            <p className="text-sm text-muted-foreground p-2">
              No facts in this cluster.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
