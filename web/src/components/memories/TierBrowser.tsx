import { useParams } from "react-router-dom";
import { useMemories } from "@/hooks/useMemories";
import { SummaryCard } from "./SummaryCard";
import { ClusterCard } from "./ClusterCard";
import { MemoryFilters } from "./MemoryFilters";

export function TierBrowser() {
  const { id } = useParams<{ id: string }>();
  const { summary, clusters, facts, filters, setFilters, isLoading } =
    useMemories(id ?? "");

  if (isLoading) {
    return (
      <div className="p-6 text-center text-base text-muted-foreground">Loading memories...</div>
    );
  }

  return (
    <div className="p-4 sm:p-6 space-y-5 animate-fade-in max-w-6xl mx-auto">
      <SummaryCard summary={summary} />

      <div className="space-y-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h3 className="font-heading text-[28px] leading-tight text-foreground">
              Topic Clusters
            </h3>
            <p className="text-sm text-muted-foreground mt-1">
              Explore grouped decisions, context, and atomic facts from this channel.
            </p>
          </div>
          <span className="text-sm text-muted-foreground">
            {clusters.length} clusters &middot; {facts.length} matching facts
          </span>
        </div>

        <MemoryFilters filters={filters} setFilters={setFilters} />

        <div className="space-y-3">
          {clusters.map((cluster, idx) => (
            <div
              key={cluster.id}
              className="motion-safe:animate-rise-in"
              style={{ animationDelay: `${Math.min(idx, 10) * 35}ms` }}
            >
              <ClusterCard cluster={cluster} facts={facts} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
