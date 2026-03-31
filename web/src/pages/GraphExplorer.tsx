import { useState, useEffect } from "react";
import { useGraph } from "@/hooks/useGraph";
import { GraphFilters } from "@/components/graph/GraphFilters";
import { GraphCanvas } from "@/components/graph/GraphCanvas";
import { EntityPanel } from "@/components/graph/EntityPanel";

export function GraphExplorer() {
  const [channelId] = useState("");
  const { entities, relationships, loading, error } = useGraph(channelId);
  const [visibleTypes, setVisibleTypes] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Derive entity types from data; keep all visible when types change
  const entityTypes = [...new Set(entities.map((e) => e.type))].sort();

  useEffect(() => {
    setVisibleTypes(entityTypes);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entities]);

  const selectedEntity = selectedId
    ? entities.find((e) => e.id === selectedId) ?? null
    : null;

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-6 py-4 border-b border-border bg-background">
        <h1 className="text-lg font-semibold text-foreground">Graph Explorer</h1>
        <p className="text-sm text-muted-foreground mt-0.5">
          Browse entities and relationships across all channels.
        </p>
      </div>

      <GraphFilters entityTypes={entityTypes} selected={visibleTypes} onChange={setVisibleTypes} />

      {loading && (
        <div className="flex items-center justify-center flex-1 p-6">
          <p className="text-sm text-muted-foreground">Loading graph...</p>
        </div>
      )}

      {error && (
        <div className="flex items-center justify-center flex-1 p-6">
          <p className="text-sm text-destructive">{error}</p>
        </div>
      )}

      {!loading && !error && (
        <div className="flex flex-1 min-h-0">
          <GraphCanvas
            entities={entities}
            relationships={relationships}
            visibleTypes={visibleTypes}
            selectedEntityId={selectedId}
            onSelectEntity={setSelectedId}
          />
          {selectedEntity && (
            <EntityPanel
              entity={selectedEntity}
              relationships={relationships}
              allEntities={entities}
              onClose={() => setSelectedId(null)}
            />
          )}
        </div>
      )}
    </div>
  );
}
