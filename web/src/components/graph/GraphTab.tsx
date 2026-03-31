import { useState, useEffect } from "react";
import { useParams } from "react-router-dom";
import { useGraph } from "@/hooks/useGraph";
import { GraphFilters } from "./GraphFilters";
import { GraphCanvas } from "./GraphCanvas";
import { EntityPanel } from "./EntityPanel";

export function GraphTab() {
  const { id: channelId } = useParams<{ id: string }>();
  const { entities, relationships, loading, error } = useGraph(channelId ?? "");
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

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full p-6">
        <p className="text-sm text-muted-foreground">Loading graph...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full p-6">
        <p className="text-sm text-destructive">{error}</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col min-h-0" style={{ height: "calc(100vh - 132px)" }}>
      <GraphFilters entityTypes={entityTypes} selected={visibleTypes} onChange={setVisibleTypes} />
      <div className="flex flex-1 min-h-0 overflow-hidden">
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
    </div>
  );
}
