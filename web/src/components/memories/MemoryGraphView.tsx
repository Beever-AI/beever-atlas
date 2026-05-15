import { useEffect, useState } from "react";
import { FolderSync, Loader2, Network, Sparkles, SlidersHorizontal, ChevronLeft } from "lucide-react";
import { useGraph } from "@/hooks/useGraph";
import { useChannelMemoryCount } from "@/hooks/useChannelMemoryCount";
import { PipelineEmptyState } from "@/components/shared/PipelineEmptyState";
import { getTypeColors } from "@/components/graph/GraphFilters";
import { GraphCanvas } from "@/components/graph/GraphCanvas";
import { EntityPanel } from "@/components/graph/EntityPanel";
import { MediaModal } from "@/components/graph/MediaModal";
import { FullscreenWrapper } from "@/components/shared/FullscreenWrapper";
import { cn } from "@/lib/utils";

const MEDIA_TYPES = new Set(["Link", "Document", "Image", "Media"]);

interface Props {
  channelId: string;
  refreshNonce?: number;
  onSyncNow?: () => Promise<void>;
  isSyncing?: boolean;
  onViewSyncHistory?: () => void;
}

// ─── TypePill ────────────────────────────────────────────────────────────────
// Compact row pill for the floating filter panel.

function TypePill({
  type,
  active,
  onToggle,
}: {
  type: string;
  active: boolean;
  onToggle: () => void;
}) {
  const colors = getTypeColors(type);
  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        "inline-flex items-center gap-2 px-2 py-1 rounded-lg text-[11px] font-medium border transition-colors w-full text-left",
        active
          ? "border-border/70 text-foreground bg-muted"
          : "border-border/40 text-muted-foreground/60 bg-transparent hover:border-border hover:text-foreground",
      )}
    >
      <span
        className="w-1.5 h-1.5 rounded-full shrink-0"
        style={{ backgroundColor: active ? colors.node : undefined }}
      />
      {type}
    </button>
  );
}

// ─── MemoryGraphView ──────────────────────────────────────────────────────────

/**
 * Entity knowledge graph rendering. Owns the cytoscape data + selection
 * + media-modal state. The parent (TierBrowser) handles the Memory↔Graph
 * view toggle in its own header.
 *
 * Filters live in a collapsible left floating panel — no top filter bar —
 * so the canvas gets the full vertical height below the segmented toggle.
 */
export function MemoryGraphView({
  channelId,
  refreshNonce = 0,
  onSyncNow,
  isSyncing = false,
  onViewSyncHistory,
}: Props) {
  const { entities, relationships, loading, error, refetch } = useGraph(channelId);
  const { hasMemories, isLoading: isMemoryCountLoading } = useChannelMemoryCount(channelId);
  const [visibleTypes, setVisibleTypes] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mediaModal, setMediaModal] = useState<{
    name: string;
    url: string;
    mediaType: string;
  } | null>(null);

  // Floating filter panel open/close
  // Filter panel defaults open — the user said the floating pill is too
  // hidden as a slim vertical strip. The panel still collapses on
  // click; the user can dismiss when they want canvas real-estate.
  const [filtersOpen, setFiltersOpen] = useState(true);

  // Orphan node toggle — GraphCanvas handles the cy.add/remove internally;
  // we just pass the boolean down.
  const [showOrphans, setShowOrphans] = useState(false);
  const [orphanCount, setOrphanCount] = useState(0);

  const entityTypes = [...new Set(entities.map((e) => e.type))].sort();

  useEffect(() => {
    setVisibleTypes(entityTypes);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entities]);

  useEffect(() => {
    if (refreshNonce > 0) {
      refetch();
    }
  }, [refreshNonce, refetch]);

  const selectedEntity = selectedId
    ? entities.find((e) => e.id === selectedId) ?? null
    : null;

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
        icon={Network}
        title={isNoMemory ? "Map relationships in this channel" : "No entities yet"}
        description={
          isNoMemory
            ? "See people, projects, and technologies connect in one visual knowledge graph."
            : "Entities will appear here once this channel's memories are consolidated into a knowledge graph."
        }
        steps={steps}
        primaryActionLabel={isNoMemory ? "Sync Channel Now" : undefined}
        onPrimaryAction={isNoMemory && onSyncNow ? () => void onSyncNow() : undefined}
        primaryActionDisabled={!onSyncNow || isSyncing}
        primaryActionLoading={isSyncing}
        secondaryActionLabel={isNoMemory ? "View sync history" : undefined}
        onSecondaryAction={isNoMemory ? onViewSyncHistory : undefined}
        secondaryActionVariant="link"
      />
    );
  }

  return (
    // No top filter bar — canvas fills full height below Memory↔Graph toggle.
    <FullscreenWrapper label="Enlarge graph" className="flex-1 min-h-0">
      <div className="relative flex h-full w-full min-h-0 overflow-hidden">
        <GraphCanvas
          entities={entities}
          relationships={relationships}
          visibleTypes={visibleTypes}
          showOrphans={showOrphans}
          selectedEntityId={selectedId}
          onSelectEntity={handleSelectEntity}
          onOrphanCount={setOrphanCount}
        />

        {selectedEntity && (
          <EntityPanel
            entity={selectedEntity}
            relationships={relationships}
            allEntities={entities}
            channelId={channelId}
            onClose={() => setSelectedId(null)}
          />
        )}

        {/* ── Floating left filter panel ─────────────────────────────── */}
        <div className="absolute left-3 top-1/2 -translate-y-1/2 z-20">
          {!filtersOpen ? (
            // Collapsed: slim vertical pill with icon + rotated label
            <button
              type="button"
              onClick={() => setFiltersOpen(true)}
              className={cn(
                "flex flex-col items-center gap-1.5 rounded-xl border border-border/60",
                "bg-card/85 backdrop-blur-sm px-2 py-3 shadow-sm",
                "text-muted-foreground hover:text-foreground hover:bg-card transition-colors",
              )}
              aria-label="Open graph filters"
            >
              <SlidersHorizontal className="w-3.5 h-3.5" />
              <span
                className="text-[9px] font-medium tracking-wider uppercase text-muted-foreground/70"
                style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
              >
                Filters
              </span>
              {/* Badge: how many types are hidden */}
              {visibleTypes.length < entityTypes.length && (
                <span className="flex h-4 w-4 items-center justify-center rounded-full bg-primary text-[8px] font-bold text-primary-foreground leading-none">
                  {entityTypes.length - visibleTypes.length}
                </span>
              )}
            </button>
          ) : (
            // Expanded panel
            <div
              className={cn(
                "flex flex-col gap-2 rounded-xl border border-border/60",
                "bg-card/92 backdrop-blur-sm shadow-md p-3 min-w-[148px] max-w-[180px]",
              )}
            >
              {/* Header */}
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/70">
                  <SlidersHorizontal className="w-3 h-3" />
                  Filters
                </span>
                <button
                  type="button"
                  onClick={() => setFiltersOpen(false)}
                  className="text-muted-foreground/50 hover:text-foreground transition-colors"
                  aria-label="Close filters"
                >
                  <ChevronLeft className="w-3.5 h-3.5" />
                </button>
              </div>

              {/* Type pills column */}
              <div className="flex flex-col gap-1">
                {entityTypes.map((type) => (
                  <TypePill
                    key={type}
                    type={type}
                    active={visibleTypes.includes(type)}
                    onToggle={() => {
                      if (visibleTypes.includes(type)) {
                        setVisibleTypes((v) => v.filter((t) => t !== type));
                      } else {
                        setVisibleTypes((v) => [...v, type]);
                      }
                    }}
                  />
                ))}
              </div>

              {/* Show all shortcut */}
              {visibleTypes.length < entityTypes.length && (
                <button
                  type="button"
                  onClick={() => setVisibleTypes([...entityTypes])}
                  className="text-[10px] text-muted-foreground/60 hover:text-foreground transition-colors text-left"
                >
                  Show all
                </button>
              )}

              <div className="border-t border-border/40" />

              {/* Orphan toggle */}
              <button
                type="button"
                onClick={() => setShowOrphans((v) => !v)}
                disabled={orphanCount === 0}
                className={cn(
                  "inline-flex items-center gap-2 px-2 py-1 rounded-lg text-[11px] font-medium border transition-colors",
                  "disabled:opacity-40 disabled:cursor-not-allowed",
                  showOrphans
                    ? "border-border/70 text-foreground bg-muted"
                    : "border-border/40 text-muted-foreground/60 bg-transparent hover:enabled:border-border hover:enabled:text-foreground",
                )}
              >
                <span
                  className={cn(
                    "w-1.5 h-1.5 rounded-full shrink-0 transition-colors",
                    showOrphans ? "bg-muted-foreground" : "bg-muted-foreground/35",
                  )}
                />
                {orphanCount > 0 ? `${orphanCount} unconnected` : "No orphans"}
              </button>
            </div>
          )}
        </div>
        {/* ── End floating filter panel ──────────────────────────────── */}
      </div>

      {mediaModal && (
        <MediaModal
          name={mediaModal.name}
          url={mediaModal.url}
          mediaType={mediaModal.mediaType}
          onClose={() => setMediaModal(null)}
        />
      )}
    </FullscreenWrapper>
  );
}
