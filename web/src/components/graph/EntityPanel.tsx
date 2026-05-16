import { useState, useEffect } from "react";
import { X } from "lucide-react";
import type { GraphEntity, GraphRelationship } from "@/hooks/useGraph";
import { useEntityFacts } from "@/hooks/useEntityFacts";
import { useEntityCard } from "@/hooks/useEntityCard";
import { FactCard } from "@/components/memories/FactCard";
import { cn } from "@/lib/utils";
import { getTypeColors } from "./GraphFilters";

type PanelTab = "card" | "details" | "facts";

interface EntityPanelProps {
  entity: GraphEntity;
  relationships: GraphRelationship[];
  allEntities: GraphEntity[];
  channelId: string;
  onClose: () => void;
  /** Optional: clicking a related entity in the Card tab pivots the
   *  parent's selectedId to a new entity (by name). The parent is
   *  responsible for name→id resolution; this panel just announces. */
  onNavigate?: (name: string) => void;
  /** Which tab opens first; defaults to "card" since graph-click is the
   *  primary entry point and the card is the headline content. */
  defaultTab?: PanelTab;
}

export function EntityPanel({
  entity,
  relationships,
  allEntities,
  channelId,
  onClose,
  onNavigate,
  defaultTab = "card",
}: EntityPanelProps) {
  const [activeTab, setActiveTab] = useState<PanelTab>(defaultTab);

  // Reset tab when entity changes
  useEffect(() => {
    setActiveTab(defaultTab);
  }, [entity.id, defaultTab]);

  // ESC closes the panel (document-level listener, scoped to mount).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const {
    card,
    loading: cardLoading,
    error: cardError,
    notFound: cardNotFound,
  } = useEntityCard(activeTab === "card" ? entity.name : null);

  const { facts, total, loading: factsLoading } = useEntityFacts(
    channelId,
    entity.name,
    activeTab === "facts",
  );

  const connected = relationships.filter(
    (r) => r.source_id === entity.id || r.target_id === entity.id,
  );

  function resolveEntityName(id: string): string {
    return allEntities.find((e) => e.id === id)?.name ?? id;
  }

  const properties = entity.properties
    ? Object.entries(entity.properties).filter(([, v]) => v != null)
    : [];

  const aliases = entity.aliases ?? [];

  // Build a lower-case name set so we can flag related-entity links as
  // "cross-channel" (not loaded in the current canvas).
  const loadedNameSet = new Set(allEntities.map((e) => e.name.toLowerCase()));

  return (
    <div className="w-full sm:w-96 shrink-0 border-l border-border bg-card flex flex-col overflow-hidden absolute sm:relative inset-0 sm:inset-auto z-10 sm:z-auto">
      {/* Header */}
      <div className="flex items-start justify-between gap-2 px-4 py-3 border-b border-border">
        <div className="min-w-0">
          <h3 className="font-semibold text-sm text-foreground truncate">
            {entity.name}
          </h3>
          <div className="flex items-center gap-1.5 mt-1">
            <span
              className={cn(
                "inline-flex px-2 py-0.5 rounded-md text-xs font-medium",
                getTypeColors(entity.type).pill,
              )}
            >
              {entity.type}
            </span>
            {entity.scope && (
              <span className="text-xs text-muted-foreground truncate">
                {entity.scope}
              </span>
            )}
          </div>
        </div>
        <button
          onClick={onClose}
          className="shrink-0 w-6 h-6 flex items-center justify-center rounded-md hover:bg-muted transition-colors text-muted-foreground"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* Tab bar */}
      <div className="flex border-b border-border">
        <button
          onClick={() => setActiveTab("card")}
          className={cn(
            "flex-1 px-3 py-2 text-xs font-medium transition-colors",
            activeTab === "card"
              ? "text-foreground border-b-2 border-primary"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          Card
        </button>
        <button
          onClick={() => setActiveTab("details")}
          className={cn(
            "flex-1 px-3 py-2 text-xs font-medium transition-colors",
            activeTab === "details"
              ? "text-foreground border-b-2 border-primary"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          Details
        </button>
        <button
          onClick={() => setActiveTab("facts")}
          className={cn(
            "flex-1 px-3 py-2 text-xs font-medium transition-colors",
            activeTab === "facts"
              ? "text-foreground border-b-2 border-primary"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          Facts{total > 0 ? ` (${total})` : ""}
        </button>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto">
        {activeTab === "card" && (
          <div className="divide-y divide-border">
            {cardLoading && (
              <div className="px-4 py-4 space-y-2" aria-label="Loading entity card">
                <div className="h-3 rounded bg-muted animate-pulse" />
                <div className="h-3 rounded bg-muted animate-pulse w-5/6" />
                <div className="h-3 rounded bg-muted animate-pulse w-4/6" />
              </div>
            )}

            {!cardLoading && cardNotFound && (
              <div className="px-4 py-6 text-center">
                <p className="text-xs text-muted-foreground">
                  No knowledge card has been generated for this entity yet.
                </p>
              </div>
            )}

            {!cardLoading && cardError && (
              <div className="px-4 py-6 text-center">
                <p className="text-xs text-destructive">{cardError}</p>
              </div>
            )}

            {!cardLoading && card && (
              <>
                {/* Summary / bio */}
                {card.summary && (
                  <section className="px-4 py-3">
                    <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
                      Summary
                    </p>
                    <p className="text-xs text-foreground leading-relaxed whitespace-pre-wrap">
                      {card.summary}
                    </p>
                  </section>
                )}

                {/* Aliases (also surfaced here for parity with the Details tab) */}
                {aliases.length > 0 && (
                  <section className="px-4 py-3">
                    <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
                      Aliases
                    </p>
                    <div className="flex flex-wrap gap-1">
                      {aliases.map((alias) => (
                        <span
                          key={alias}
                          className="px-2 py-0.5 rounded-md bg-muted text-xs text-muted-foreground"
                        >
                          {alias}
                        </span>
                      ))}
                    </div>
                  </section>
                )}

                {/* Key facts */}
                {card.key_facts.length > 0 && (
                  <section className="px-4 py-3">
                    <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
                      Key facts
                    </p>
                    <ul className="space-y-1.5 list-disc list-inside">
                      {card.key_facts.map((fact, i) => (
                        <li key={i} className="text-xs text-foreground leading-snug">
                          {fact}
                        </li>
                      ))}
                    </ul>
                  </section>
                )}

                {/* Related entities */}
                {card.related_entities.length > 0 && (
                  <section className="px-4 py-3">
                    <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
                      Related entities
                    </p>
                    <ul className="space-y-1">
                      {card.related_entities.map((rel, i) => {
                        const name = typeof rel.name === "string" ? rel.name : null;
                        const type = typeof rel.type === "string" ? rel.type : null;
                        if (!name) return null;
                        const isLoaded = loadedNameSet.has(name.toLowerCase());
                        return (
                          <li key={`${name}-${i}`} className="flex items-center gap-2">
                            <button
                              type="button"
                              onClick={() => isLoaded && onNavigate?.(name)}
                              disabled={!isLoaded}
                              className={cn(
                                "text-left text-xs flex-1 min-w-0 truncate transition-colors",
                                isLoaded
                                  ? "text-foreground hover:text-primary cursor-pointer underline-offset-2 hover:underline"
                                  : "text-muted-foreground cursor-not-allowed",
                              )}
                              title={
                                isLoaded
                                  ? `Open ${name}`
                                  : `${name} is not loaded in the current channel`
                              }
                            >
                              {name}
                            </button>
                            {type && (
                              <span className="text-[10px] text-muted-foreground shrink-0">
                                {type}
                              </span>
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  </section>
                )}

                {/* Citations: surfaced via fact_count + breakdown for now;
                    `source_message_id` links are out-of-scope until the
                    fact extractor pins a citation token format (see plan
                    §C.4.2 C-2 deferral). */}
                {card.fact_count > 0 && (
                  <section className="px-4 py-3">
                    <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
                      Citations
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {card.fact_count} supporting fact
                      {card.fact_count === 1 ? "" : "s"}
                      {card.last_mentioned_at
                        ? ` · last mentioned ${card.last_mentioned_at.slice(0, 10)}`
                        : ""}
                    </p>
                  </section>
                )}

                {!card.summary &&
                  card.key_facts.length === 0 &&
                  card.related_entities.length === 0 &&
                  aliases.length === 0 && (
                    <div className="px-4 py-6 text-center">
                      <p className="text-xs text-muted-foreground">
                        This entity has a card but no summary or related entities yet.
                      </p>
                    </div>
                  )}
              </>
            )}
          </div>
        )}

        {activeTab === "details" && (
          <div className="divide-y divide-border">
            {/* Aliases */}
            {aliases.length > 0 && (
              <section className="px-4 py-3">
                <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
                  Aliases
                </p>
                <div className="flex flex-wrap gap-1">
                  {aliases.map((alias) => (
                    <span
                      key={alias}
                      className="px-2 py-0.5 rounded-md bg-muted text-xs text-muted-foreground"
                    >
                      {alias}
                    </span>
                  ))}
                </div>
              </section>
            )}

            {/* Properties */}
            {properties.length > 0 && (
              <section className="px-4 py-3">
                <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
                  Properties
                </p>
                <dl className="space-y-1.5">
                  {properties.map(([key, val]) => (
                    <div key={key} className="flex gap-2">
                      <dt className="text-xs text-muted-foreground shrink-0 w-24 truncate capitalize">
                        {key.replace(/_/g, " ")}
                      </dt>
                      <dd className="text-xs text-foreground break-words min-w-0">
                        {String(val)}
                      </dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}

            {/* Relationships */}
            {connected.length > 0 && (
              <section className="px-4 py-3">
                <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
                  Relationships ({connected.length})
                </p>
                <ul className="space-y-2">
                  {connected.map((rel) => {
                    const isSource = rel.source_id === entity.id;
                    const otherId = isSource ? rel.target_id : rel.source_id;
                    const otherName = resolveEntityName(otherId);
                    return (
                      <li key={rel.id} className="flex items-start gap-2">
                        <span className="text-xs text-muted-foreground shrink-0 mt-0.5">
                          {isSource ? "→" : "←"}
                        </span>
                        <div className="min-w-0">
                          <span className="text-xs font-medium text-foreground block truncate">
                            {otherName}
                          </span>
                          <span className="text-xs text-muted-foreground">
                            {rel.type}
                          </span>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </section>
            )}

            {properties.length === 0 && aliases.length === 0 && connected.length === 0 && (
              <div className="px-4 py-6 text-center">
                <p className="text-xs text-muted-foreground">No details available.</p>
              </div>
            )}
          </div>
        )}

        {activeTab === "facts" && (
          <div className="p-3 space-y-2">
            {factsLoading && (
              <p className="text-xs text-muted-foreground text-center py-4">Loading facts...</p>
            )}
            {!factsLoading && facts.length === 0 && (
              <div className="text-center py-6">
                <p className="text-xs text-muted-foreground">No facts found for this entity.</p>
              </div>
            )}
            {facts.map((fact) => (
              <FactCard key={fact.id} fact={fact} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
