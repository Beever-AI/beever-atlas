import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  FolderSync,
  Loader2,
  Network,
  Search,
  SlidersHorizontal,
  Sparkles,
  Waypoints,
} from "lucide-react";
import { useGraph } from "@/hooks/useGraph";
import { useChannelMemoryCount } from "@/hooks/useChannelMemoryCount";
import { PipelineEmptyState } from "@/components/shared/PipelineEmptyState";
import { getTypeColors } from "@/components/graph/GraphFilters";
import { GraphCanvas, type GraphCanvasHandle } from "@/components/graph/GraphCanvas";
import { EntityPanel } from "@/components/graph/EntityPanel";
import { MediaModal } from "@/components/graph/MediaModal";
import { EntitySearchPalette } from "@/components/graph/EntitySearchPalette";
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

/** Possible time-window values for the D-track filter pills. `null`
 *  represents "All time" (the default — pre-D behavior). */
type TimeWindow = 7 | 30 | 90 | null;

const TIME_WINDOWS: { value: TimeWindow; label: string }[] = [
  { value: 7, label: "7d" },
  { value: 30, label: "30d" },
  { value: 90, label: "90d" },
  { value: null, label: "All" },
];

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

// ─── TimePill ────────────────────────────────────────────────────────────────
// Compact pill for the time-window row. Active pill is filled.

function TimePill({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "px-2.5 py-1 rounded-lg text-[11px] font-medium border transition-colors",
        active
          ? "border-border/70 text-foreground bg-muted"
          : "border-border/40 text-muted-foreground/60 bg-transparent hover:border-border hover:text-foreground",
      )}
      aria-pressed={active}
    >
      {label}
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
  // Loose-connections toggle: when on, the relationships endpoint
  // surfaces co-mention edges at the weakest threshold (shared >= 1
  // event between two entities). Helps sparse channels where the
  // default shared >= 2 leaves many entities isolated. Defaults off
  // so dense channels stay visually clean.
  const [looseConnections, setLooseConnections] = useState(false);
  const { entities, relationships, loading, error, refetch } = useGraph(channelId, {
    looseConnections,
  });
  const { hasMemories, isLoading: isMemoryCountLoading } = useChannelMemoryCount(channelId);
  const [visibleTypes, setVisibleTypes] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mediaModal, setMediaModal] = useState<{
    name: string;
    url: string;
    mediaType: string;
  } | null>(null);

  // D — time-window pills. `null` means "all time" (pre-D behavior).
  const [timeWindow, setTimeWindow] = useState<TimeWindow>(null);

  // E-3 — cmd-K palette open/close
  const [paletteOpen, setPaletteOpen] = useState(false);

  // Imperative ref into the canvas — used by the palette to pan/zoom
  // to the selected entity.
  const canvasRef = useRef<GraphCanvasHandle | null>(null);

  // Floating filter panel open/close
  // Filter panel defaults open — the user said the floating pill is too
  // hidden as a slim vertical strip. The panel still collapses on
  // click; the user can dismiss when they want canvas real-estate.
  // Default collapsed so the panel doesn't steal canvas real estate on
  // first paint. Users can open it via the slim left rail.
  const [filtersOpen, setFiltersOpen] = useState(false);

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

  // E-3 (E-1 fix) — document-level cmd-K (or ctrl-K) hotkey toggle.
  // Bound at document level so it works regardless of focus target.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const isHotkey = (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k";
      if (!isHotkey) return;
      e.preventDefault();
      setPaletteOpen((open) => !open);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // D — client-side time filter. Null timeWindow keeps everything; for
  // other windows, edges WITHOUT a `valid_from` stay visible (treated
  // as "always visible" / "legacy"), edges with a stale timestamp drop.
  const filteredRelationships = useMemo(() => {
    if (timeWindow === null) return relationships;
    const cutoff = Date.now() - timeWindow * 86400_000;
    return relationships.filter((r) => {
      if (!r.valid_from) return true; // null = always visible
      const t = Date.parse(r.valid_from);
      if (!Number.isFinite(t)) return true;
      return t >= cutoff;
    });
  }, [relationships, timeWindow]);

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

  // C — navigate by name (from EntityPanel's Card tab's related entities)
  const handleNavigate = (name: string) => {
    const target = entities.find(
      (e) => e.name.toLowerCase() === name.toLowerCase(),
    );
    if (target) {
      setSelectedId(target.id);
      // Pan the canvas to the new entity so the user has visual context.
      canvasRef.current?.focusNode(target.id);
    }
  };

  // E-3 — palette selection routes through focusNode + selection state.
  const handlePaletteSelect = (id: string) => {
    setSelectedId(id);
    canvasRef.current?.focusNode(id);
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

  const timeFilterEmpty =
    timeWindow !== null &&
    filteredRelationships.length === 0 &&
    relationships.length > 0;

  return (
    // No top filter bar — canvas fills full height below Memory↔Graph toggle.
    <FullscreenWrapper label="Enlarge graph" className="flex-1 min-h-0">
      <div className="relative flex h-full w-full min-h-0 overflow-hidden">
        <GraphCanvas
          ref={canvasRef}
          entities={entities}
          relationships={filteredRelationships}
          visibleTypes={visibleTypes}
          showOrphans={showOrphans}
          selectedEntityId={selectedId}
          onSelectEntity={handleSelectEntity}
          onOrphanCount={setOrphanCount}
        />

        {selectedEntity && (
          <EntityPanel
            entity={selectedEntity}
            relationships={filteredRelationships}
            allEntities={entities}
            channelId={channelId}
            onClose={() => setSelectedId(null)}
            onNavigate={handleNavigate}
          />
        )}

        {/* ── Top-right toolbar: search + time-window pills ──────────── */}
        <div className="absolute top-3 right-3 z-20 flex items-center gap-2">
          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            aria-label="Search entities (⌘K)"
            title="Search entities (⌘K)"
            className={cn(
              "inline-flex items-center justify-center w-8 h-8 rounded-xl",
              "border border-border/60 bg-card/85 backdrop-blur-sm shadow-sm",
              "text-muted-foreground hover:text-foreground hover:bg-card transition-colors",
            )}
          >
            <Search className="w-3.5 h-3.5" />
          </button>
          {/* Co-mention toggle. When ON, the server surfaces co-mention
              edges at the weakest threshold (shared >= 1) so sparse
              channels show a more connected graph. Labeled "Show all
              co-mentions" with explanatory tooltip so users don't have
              to guess what "Loose" means. */}
          <button
            type="button"
            onClick={() => setLooseConnections((v) => !v)}
            aria-pressed={looseConnections}
            aria-label={
              looseConnections
                ? "Hide weak co-mention edges"
                : "Show all co-mention edges, including single-mention pairs"
            }
            title={
              looseConnections
                ? (
                  "Showing ALL co-mentions: any two entities that appear " +
                  "in the same fact get a dashed link. Click to hide weak " +
                  "ones (only show pairs appearing in 2+ shared facts)."
                )
                : (
                  "Click to include weak co-mentions: link any two entities " +
                  "that ever appear in the same fact. Useful for sparse " +
                  "channels where most entities look isolated."
                )
            }
            className={cn(
              "inline-flex items-center gap-1.5 px-2.5 h-8 rounded-xl border shadow-sm",
              "backdrop-blur-sm transition-colors text-[11px] font-medium",
              looseConnections
                ? "border-primary/60 bg-primary/15 text-primary hover:bg-primary/20"
                : "border-border/60 bg-card/85 text-muted-foreground hover:text-foreground hover:bg-card",
            )}
          >
            <Waypoints className="w-3.5 h-3.5" aria-hidden="true" />
            <span>{looseConnections ? "All co-mentions" : "Show weak ties"}</span>
          </button>
          <div
            className={cn(
              "inline-flex items-center gap-1 rounded-xl border border-border/60",
              "bg-card/85 backdrop-blur-sm px-1.5 py-1 shadow-sm",
            )}
          >
            {TIME_WINDOWS.map((w) => (
              <TimePill
                key={w.label}
                label={w.label}
                active={timeWindow === w.value}
                onClick={() => setTimeWindow(w.value)}
              />
            ))}
          </div>
        </div>
        {/* ── End top-right toolbar ─────────────────────────────────── */}

        {/* ── Empty-window overlay ───────────────────────────────────── */}
        {timeFilterEmpty && (
          <div className="absolute inset-0 z-20 flex items-center justify-center pointer-events-none">
            <div className="pointer-events-auto rounded-xl border border-border/60 bg-card/95 backdrop-blur-sm shadow-md px-5 py-4 max-w-sm text-center">
              <p className="text-sm font-medium text-foreground mb-1">
                No activity in the last {timeWindow} days
              </p>
              <p className="text-xs text-muted-foreground mb-3">
                Try a longer window to see older relationships.
              </p>
              <button
                type="button"
                onClick={() => setTimeWindow(null)}
                className="inline-flex items-center px-3 py-1.5 rounded-lg text-xs font-medium border border-border/70 text-foreground bg-muted hover:bg-muted/80 transition-colors"
              >
                Show all time
              </button>
            </div>
          </div>
        )}
        {/* ── End empty-window overlay ──────────────────────────────── */}

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

        {/* ── Footer count pill ─────────────────────────────────────── */}
        <div className="absolute bottom-3 right-3 z-20 pointer-events-none">
          <div
            className={cn(
              "inline-flex items-center gap-1 rounded-full border border-border/60",
              "bg-card/85 backdrop-blur-sm px-2.5 py-1 shadow-sm",
              "text-[10px] font-medium text-muted-foreground/80",
            )}
          >
            {entities.length} entities · {filteredRelationships.length} relationships
            {timeWindow !== null && (
              <span className="ml-1 text-muted-foreground/60">
                · last {timeWindow}d
              </span>
            )}
          </div>
        </div>
        {/* ── End footer count pill ─────────────────────────────────── */}
      </div>

      {mediaModal && (
        <MediaModal
          name={mediaModal.name}
          url={mediaModal.url}
          mediaType={mediaModal.mediaType}
          onClose={() => setMediaModal(null)}
        />
      )}

      <EntitySearchPalette
        entities={entities}
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onSelect={handlePaletteSelect}
      />
    </FullscreenWrapper>
  );
}
