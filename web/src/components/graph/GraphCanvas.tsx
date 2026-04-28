import { useEffect, useLayoutEffect, useRef } from "react";
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

  // Keep a stable ref to the latest onSelectEntity to avoid stale closures
  // in cytoscape event handlers (the main useEffect doesn't include
  // onSelectEntity in its deps to avoid destroying cytoscape on every render).
  const onSelectRef = useRef(onSelectEntity);
  useLayoutEffect(() => {
    onSelectRef.current = onSelectEntity;
  });

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
      const isPending = e.status === "pending";
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
          pending: isPending,
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

    // Detect dark mode for edge label theming
    const isDark = document.documentElement.classList.contains("dark");
    const edgeLabelColor = isDark ? "#cbd5e1" : "#475569";
    const edgeLabelBg = isDark ? "#1e293b" : "#f8fafc";
    const edgeLineColor = isDark ? "#475569" : "#cbd5e1";
    const edgeHoverColor = isDark ? "#94a3b8" : "#64748b";
    const edgeHoverLabelColor = isDark ? "#e2e8f0" : "#334155";
    const edgeHighlightColor = isDark ? "#38bdf8" : "#0B4F6C";

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
          selector: "node[?pending]",
          style: {
            "border-style": "dashed" as const,
            opacity: 0.5,
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
            "line-color": edgeLineColor,
            "target-arrow-color": edgeLineColor,
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.7,
            "curve-style": "bezier",
            label: "data(label)",
            "font-size": "8px",
            color: edgeLabelColor,
            "text-rotation": "autorotate",
            "text-margin-y": -6,
            "text-background-color": edgeLabelBg,
            "text-background-opacity": 0.9,
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
            "line-color": edgeHoverColor,
            "target-arrow-color": edgeHoverColor,
            "font-size": "9px",
            color: edgeHoverLabelColor,
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
            "line-color": edgeHighlightColor,
            "target-arrow-color": edgeHighlightColor,
            "font-size": "9px",
            color: edgeHighlightColor,
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

    // --- Physics: spring pull on connected nodes when dragging + momentum ---
    let dragTarget: cytoscape.NodeSingular | null = null;
    // Track velocity per neighbor for momentum after release
    const velocities = new Map<string, { vx: number; vy: number }>();
    let momentumFrame: number | null = null;

    cy.on("grab", "node", (evt) => {
      dragTarget = evt.target;
      velocities.clear();
      if (momentumFrame) {
        cancelAnimationFrame(momentumFrame);
        momentumFrame = null;
      }
    });

    cy.on("drag", "node", () => {
      if (!dragTarget) return;
      const pos = dragTarget.position();
      dragTarget.neighborhood("node").forEach((neighbor) => {
        const nPos = neighbor.position();
        const dx = pos.x - nPos.x;
        const dy = pos.y - nPos.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 40) return;
        const force = Math.min(0.02, 80 / (dist * dist));
        const moveX = dx * force;
        const moveY = dy * force;
        neighbor.position({
          x: nPos.x + moveX,
          y: nPos.y + moveY,
        });
        // Accumulate velocity for momentum
        const prev = velocities.get(neighbor.id()) || { vx: 0, vy: 0 };
        velocities.set(neighbor.id(), {
          vx: prev.vx * 0.5 + moveX * 8,
          vy: prev.vy * 0.5 + moveY * 8,
        });
      });
    });

    cy.on("free", "node", () => {
      dragTarget = null;

      // Apply momentum: neighbors drift with decaying velocity
      const friction = 0.88;
      const minSpeed = 0.3;

      const step = () => {
        let anyMoving = false;
        velocities.forEach((vel, nodeId) => {
          if (Math.abs(vel.vx) < minSpeed && Math.abs(vel.vy) < minSpeed) return;
          const node = cy.getElementById(nodeId);
          if (!node.length) return;
          const p = node.position();
          node.position({ x: p.x + vel.vx, y: p.y + vel.vy });
          vel.vx *= friction;
          vel.vy *= friction;
          anyMoving = true;
        });
        if (anyMoving) {
          momentumFrame = requestAnimationFrame(step);
        } else {
          momentumFrame = null;
          // Save final positions after momentum settles
          cy.nodes().forEach((n) => {
            const p = n.position();
            positionCache.set(n.id(), { x: p.x, y: p.y });
          });
        }
      };
      momentumFrame = requestAnimationFrame(step);

      // Also save dragged node position immediately
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
      onSelectRef.current(node.id());
    });

    cy.on("tap", (evt) => {
      if (evt.target === cy) {
        cy.elements().removeClass("dimmed highlighted hover");
        onSelectRef.current(null);
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
      if (momentumFrame) cancelAnimationFrame(momentumFrame);
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
      className="flex-1 min-h-0 bg-muted/5 overflow-hidden"
    />
  );
}
