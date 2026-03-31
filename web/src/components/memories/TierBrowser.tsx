import { useParams } from "react-router-dom";
import { useMemories } from "@/hooks/useMemories";
import { MemoryFilters } from "./MemoryFilters";
import { FactCard } from "./FactCard";

export function TierBrowser() {
  const { id } = useParams<{ id: string }>();
  const { facts, filters, setFilters, isLoading } = useMemories(id ?? "");

  if (isLoading) {
    return (
      <div className="p-6 text-center text-base text-muted-foreground">
        Loading memories...
      </div>
    );
  }

  return (
    <div className="p-4 sm:p-6 space-y-5 animate-fade-in max-w-6xl mx-auto">
      {/* Tier 0 — Summary */}
      <div className="rounded-xl border border-border bg-card px-5 py-4 text-sm text-muted-foreground">
        Channel summary — coming in M5
      </div>

      {/* Tier 1 — Clusters */}
      <div className="rounded-xl border border-border bg-card px-5 py-4 text-sm text-muted-foreground">
        Topic clusters — coming in M5
      </div>

      {/* Tier 2 — Atomic facts */}
      <div className="space-y-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h3 className="font-heading text-[28px] leading-tight text-foreground">
              Atomic Facts
            </h3>
            <p className="text-sm text-muted-foreground mt-1">
              Individual knowledge extracted from this channel.
            </p>
          </div>
          <span className="text-sm text-muted-foreground">
            {facts.length} matching facts
          </span>
        </div>

        <MemoryFilters filters={filters} setFilters={setFilters} />

        {facts.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border px-5 py-10 text-center text-sm text-muted-foreground">
            No memories yet. Sync this channel to start extracting knowledge.
          </div>
        ) : (
          <div className="space-y-3">
            {facts.map((fact, idx) => (
              <div
                key={fact.id}
                className="motion-safe:animate-rise-in"
                style={{ animationDelay: `${Math.min(idx, 10) * 35}ms` }}
              >
                <FactCard fact={fact} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
