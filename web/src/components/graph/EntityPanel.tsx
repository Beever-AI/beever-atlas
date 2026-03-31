import { X } from "lucide-react";
import type { GraphEntity, GraphRelationship } from "@/hooks/useGraph";
import { cn } from "@/lib/utils";
import { getTypeColors } from "./GraphFilters";

interface EntityPanelProps {
  entity: GraphEntity;
  relationships: GraphRelationship[];
  allEntities: GraphEntity[];
  onClose: () => void;
}

export function EntityPanel({
  entity,
  relationships,
  allEntities,
  onClose,
}: EntityPanelProps) {
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

  return (
    <div className="w-72 shrink-0 border-l border-border bg-card flex flex-col overflow-hidden">
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

      <div className="flex-1 overflow-y-auto divide-y divide-border">
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
    </div>
  );
}
