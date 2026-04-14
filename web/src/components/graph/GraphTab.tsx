import { useState, useEffect } from "react";
import { useParams } from "react-router-dom";
import { FolderSync, Loader2, Network, Sparkles } from "lucide-react";
import { useGraph } from "@/hooks/useGraph";
import { useChannelMemoryCount } from "@/hooks/useChannelMemoryCount";
import { PipelineEmptyState } from "@/components/shared/PipelineEmptyState";
import { GraphFilters } from "./GraphFilters";
import { GraphCanvas } from "./GraphCanvas";
import { EntityPanel } from "./EntityPanel";
import { MediaModal } from "./MediaModal";

const MEDIA_TYPES = new Set(["Link", "Document", "Image", "Media"]);

export function GraphTab() {
  const { id: channelId } = useParams<{ id: string }>();
  const { entities, relationships, loading, error } = useGraph(channelId ?? "");
  const { hasMemories, isLoading: isMemoryCountLoading } = useChannelMemoryCount(channelId);
  const [visibleTypes, setVisibleTypes] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mediaModal, setMediaModal] = useState<{ name: string; url: string; mediaType: string } | null>(null);

  // Derive entity types from data; keep all visible when types change
  const entityTypes = [...new Set(entities.map((e) => e.type))].sort();

  useEffect(() => {
    setVisibleTypes(entityTypes);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entities]);

  const selectedEntity = selectedId
    ? entities.find((e) => e.id === selectedId) ?? null
    : null;

  // When a media-type node is selected, open the modal
  const handleSelectEntity = (id: string | null) => {
    if (id) {
      const entity = entities.find((e) => e.id === id);
      if (entity) {
        const props = entity.properties as Record<string, unknown> | undefined;
        const url = (props?.url as string) || "";
        const mediaType = (props?.media_type as string) || entity.type.toLowerCase();
        if (url && (MEDIA_TYPES.has(entity.type) || mediaType)) {
          setMediaModal({ name: entity.name, url, mediaType });
          return;
        }
      }
    }
    setSelectedId(id);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full p-6">
        <div className="flex flex-col items-center gap-3 text-muted-foreground">
          <Loader2 className="w-6 h-6 animate-spin" />
          <span className="text-sm">Loading graph...</span>
        </div>
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

  if (entities.length === 0 && !isMemoryCountLoading) {
    const isNoMemory = !hasMemories;
    const steps = [
      { label: "Sync channel", icon: FolderSync, done: !isNoMemory, active: isNoMemory },
      { label: "Build memories", icon: Sparkles, done: !isNoMemory, active: false },
      { label: "View graph", icon: Network, done: false, active: !isNoMemory },
    ];
    return (
      <PipelineEmptyState
        icon={isNoMemory ? FolderSync : Network}
        title={isNoMemory ? "Sync this channel first" : "No entities yet"}
        description={
          isNoMemory
            ? "The graph visualizes entities extracted from channel memories. Sync this channel to unlock it."
            : "Entities will appear here once this channel's memories are consolidated into a knowledge graph."
        }
        steps={steps}
      />
    );
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      <GraphFilters entityTypes={entityTypes} selected={visibleTypes} onChange={setVisibleTypes} />
      <div className="flex flex-1 min-h-0 overflow-hidden">
        <GraphCanvas
          entities={entities}
          relationships={relationships}
          visibleTypes={visibleTypes}
          selectedEntityId={selectedId}
          onSelectEntity={handleSelectEntity}
        />
        {selectedEntity && (
          <EntityPanel
            entity={selectedEntity}
            relationships={relationships}
            allEntities={entities}
            channelId={channelId ?? ""}
            onClose={() => setSelectedId(null)}
          />
        )}
      </div>
      {mediaModal && (
        <MediaModal
          name={mediaModal.name}
          url={mediaModal.url}
          mediaType={mediaModal.mediaType}
          onClose={() => setMediaModal(null)}
        />
      )}
    </div>
  );
}
