import { useEffect, useRef } from "react";
import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
import type { GraphEntity, GraphRelationship } from "@/hooks/useGraph";
import { getTypeColors } from "./GraphFilters";

interface GraphCanvasProps {
  entities: GraphEntity[];
  relationships: GraphRelationship[];
  visibleTypes: string[];
  onSelectEntity: (id: string | null) => void;
  selectedEntityId: string | null;
}

/** Cache of node positions keyed by entity ID for deterministic layout */
const positionCache = new Map<string, { x: number; y: number }>();

export function GraphCanvas({
  entities,
  relationships,
  visibleTypes,
  onSelectEntity,
  selectedEntityId,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);

  // Build elements whenever data changes
  useEffect(() => {
    if (!containerRef.current) return;

    const visibleSet = new Set(visibleTypes);
    const filtered = entities
      .filter((e) => visibleSet.has(e.type))
      .slice(0, 80);

    const filteredIds = new Set(filtered.map((e) => e.id));

    // Count connections per node for size scaling
    const connectionCount = new Map<string, number>();
    relationships.forEach((r) => {
      if (filteredIds.has(r.source_id) && filteredIds.has(r.target_id)) {
        connectionCount.set(r.source_id, (connectionCount.get(r.source_id) ?? 0) + 1);
        connectionCount.set(r.target_id, (connectionCount.get(r.target_id) ?? 0) + 1);
      }
    });

    // Check if we have cached positions for these nodes
    const hasCachedPositions = filtered.some((e) => positionCache.has(e.id));

    const nodes: ElementDefinition[] = filtered.map((e) => {
      const colors = getTypeColors(e.type);
      const conns = connectionCount.get(e.id) ?? 0;
      const size = Math.min(116, 58 + conns * 9);
      const cached = positionCache.get(e.id);
      const visualDesc = (e.properties as Record<string, unknown>)?.visual_description as string | undefined;
      return {
        data: {
          id: e.id,
          label: e.name,
          type: e.type,
          bgColor: colors.node,
          borderColor: colors.nodeBorder,
          nodeSize: size,
          fontSize: Math.max(10, Math.min(14, 10 + conns)),
          hasMedia: !!visualDesc,
          visualDesc: visualDesc || "",
        },
        ...(cached ? { position: cached } : {}),
      };
    });

    const edges: ElementDefinition[] = relationships
      .filter((r) => filteredIds.has(r.source_id) && filteredIds.has(r.target_id))
      .map((r, i) => ({
        data: {
          id: r.id || `edge-${i}`,
          source: r.source_id,
          target: r.target_id,
          label: r.type.replace(/_/g, " "),
        },
      }));

    if (cyRef.current) {
      cyRef.current.destroy();
    }
    const cy = cytoscape({
      container: containerRef.current,
      elements: [...nodes, ...edges],
      style: [
        {
          selector: "node",
          style: {
            "background-color": "data(bgColor)",
            "border-color": "data(borderColor)",
            "border-width": 2,
            label: "data(label)",
            color: "#ffffff",
            "font-size": "data(fontSize)",
            "font-weight": 600,
            "text-valign": "center",
            "text-halign": "center",
            "text-wrap": "wrap",
            "text-max-width": "110px",
            width: "data(nodeSize)",
            height: "data(nodeSize)",
            "text-outline-color": "data(bgColor)",
            "text-outline-width": 2,
            opacity: 1,
            "transition-property": "border-width, border-color, width, height, opacity",
            "transition-duration": "0.2s",
          } as unknown as cytoscape.Css.Node,
        },
        {
          selector: "node.selected-highlight",
          style: {
            "border-width": 4,
            "border-color": "#ffffff",
            "overlay-color": "#0B4F6C",
            "overlay-opacity": 0.15,
          },
        },
        {
          selector: "node.hover",
          style: {
            "border-width": 3,
            "border-color": "#ffffff",
            "overlay-color": "#0B4F6C",
            "overlay-opacity": 0.1,
          },
        },
        {
          selector: "node[?hasMedia]",
          style: {
            "border-style": "double" as const,
            "border-width": 4,
          },
        },
        {
          selector: "node.dimmed",
          style: { opacity: 0.35 },
        },
        {
          selector: "edge",
          style: {
            width: 1.5,
            "line-color": "#cbd5e1",
            "target-arrow-color": "#cbd5e1",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.7,
            "curve-style": "bezier",
            // Always show edge labels (relationship type is important)
            label: "data(label)",
            "font-size": "7px",
            color: "#94a3b8",
            "text-rotation": "autorotate",
            "text-margin-y": -6,
            "text-background-color": "#f8fafc",
            "text-background-opacity": 0.85,
            "text-background-padding": "2px",
            "line-style": "solid",
            opacity: 0.65,
            "transition-property": "width, line-color, opacity",
            "transition-duration": "0.2s",
          } as unknown as cytoscape.Css.Edge,
        },
        {
          selector: "edge.hover",
          style: {
            width: 2.5,
            "line-color": "#64748b",
            "target-arrow-color": "#64748b",
            "font-size": "9px",
            color: "#334155",
          },
        },
        {
          selector: "edge.dimmed",
          style: { opacity: 0.2 },
        },
        {
          selector: "edge.highlighted",
          style: {
            width: 2.5,
            "line-color": "#0B4F6C",
            "target-arrow-color": "#0B4F6C",
            "font-size": "9px",
            color: "#0B4F6C",
            opacity: 1,
          },
        },
      ],
      layout: hasCachedPositions
        ? { name: "preset", fit: true, padding: 40 }
        : {
            name: "cose",
            animate: false,
            randomize: false,
            nodeDimensionsIncludeLabels: true,
            nodeRepulsion: () => 14000,
            idealEdgeLength: () => 130,
            edgeElasticity: () => 80,
            gravity: 0.25,
            padding: 50,
            fit: true,
          } as cytoscape.LayoutOptions,
      minZoom: 0.3,
      maxZoom: 3,
      wheelSensitivity: 0.3,
    });

    // Save positions after layout for deterministic re-renders
    if (!hasCachedPositions) {
      cy.nodes().forEach((node) => {
        const pos = node.position();
        positionCache.set(node.id(), { x: pos.x, y: pos.y });
      });
    }

    // Elements start visible (opacity set in styles above).

    // --- Physics: spring pull on connected nodes when dragging ---
    let dragTarget: cytoscape.NodeSingular | null = null;

    cy.on("grab", "node", (evt) => {
      dragTarget = evt.target;
    });

    cy.on("drag", "node", () => {
      if (!dragTarget) return;
      const pos = dragTarget.position();
      // Gently pull connected neighbors toward the dragged node
      dragTarget.neighborhood("node").forEach((neighbor) => {
        const nPos = neighbor.position();
        const dx = pos.x - nPos.x;
        const dy = pos.y - nPos.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 40) return; // don't pull if already close
        const force = Math.min(0.02, 80 / (dist * dist)); // gentle pull, heavily capped
        neighbor.position({
          x: nPos.x + dx * force,
          y: nPos.y + dy * force,
        });
      });
    });

    cy.on("free", "node", () => {
      dragTarget = null;
      // Save all current positions (dragged node + pulled neighbors)
      cy.nodes().forEach((n) => {
        const p = n.position();
        positionCache.set(n.id(), { x: p.x, y: p.y });
      });
    });

    // --- Interactions ---
    // Tooltip element
    let tooltip: HTMLDivElement | null = null;

    cy.on("mouseover", "node", (evt) => {
      evt.target.addClass("hover");
      const node = evt.target;
      const type = node.data("type") as string;
      const label = node.data("label") as string;

      // Create tooltip
      if (!tooltip) {
        tooltip = document.createElement("div");
        tooltip.style.cssText =
          "position:absolute;pointer-events:none;z-index:50;padding:4px 8px;" +
          "border-radius:6px;font-size:11px;white-space:nowrap;" +
          "background:rgba(15,23,42,0.9);color:#f1f5f9;box-shadow:0 2px 8px rgba(0,0,0,0.15);";
        containerRef.current?.appendChild(tooltip);
      }
      const visualDesc = node.data("visualDesc") as string | undefined;
      if (visualDesc) {
        tooltip.textContent = `${label} · ${type}\n${visualDesc.slice(0, 100)}`;
        tooltip.style.whiteSpace = "pre-wrap";
        tooltip.style.maxWidth = "300px";
      } else {
        tooltip.textContent = `${label} · ${type}`;
        tooltip.style.whiteSpace = "nowrap";
        tooltip.style.maxWidth = "";
      }
      tooltip.style.display = "block";
    });

    cy.on("mousemove", "node", (evt) => {
      if (tooltip && containerRef.current) {
        const rect = containerRef.current.getBoundingClientRect();
        tooltip.style.left = `${evt.originalEvent.clientX - rect.left + 12}px`;
        tooltip.style.top = `${evt.originalEvent.clientY - rect.top - 28}px`;
      }
    });

    cy.on("mouseout", "node", (evt) => {
      evt.target.removeClass("hover");
      if (tooltip) {
        tooltip.style.display = "none";
      }
    });

    // Click: select + highlight neighborhood
    cy.on("tap", "node", (evt) => {
      const node = evt.target;
      const neighborhood = node.closedNeighborhood();
      cy.elements().removeClass("dimmed highlighted hover");
      cy.elements().addClass("dimmed");
      neighborhood.removeClass("dimmed");
      neighborhood.edges().addClass("highlighted");
      onSelectEntity(node.id());
    });

    cy.on("tap", (evt) => {
      if (evt.target === cy) {
        cy.elements().removeClass("dimmed highlighted hover");
        onSelectEntity(null);
      }
    });

    // Double-click: smooth zoom to neighborhood
    cy.on("dbltap", "node", (evt) => {
      const neighborhood = evt.target.closedNeighborhood();
      cy.animate({
        fit: { eles: neighborhood, padding: 60 },
        duration: 500,
        easing: "ease-in-out-cubic" as cytoscape.Css.TransitionTimingFunction,
      });
    });

    cyRef.current = cy;

    return () => {
      if (tooltip) tooltip.remove();
      cy.destroy();
      cyRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entities, relationships, visibleTypes]);

  // Highlight selected node externally
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass("dimmed highlighted");
    if (selectedEntityId) {
      const node = cy.getElementById(selectedEntityId);
      if (node.length) {
        const neighborhood = node.closedNeighborhood();
        cy.elements().addClass("dimmed");
        neighborhood.removeClass("dimmed");
        neighborhood.edges().addClass("highlighted");
      }
    }
  }, [selectedEntityId]);

  if (entities.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center bg-muted/5">
        <div className="text-center space-y-2">
          <div className="text-4xl opacity-20">🕸️</div>
          <p className="text-sm text-muted-foreground">
            No entities to display. Run a sync to populate the graph.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="flex-1 min-h-0 bg-muted/5"
    />
  );
}
