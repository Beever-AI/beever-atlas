import { useEffect, useState, useCallback } from "react";
import { Maximize2, X, ZoomIn, ZoomOut, RotateCcw } from "lucide-react";
import mermaid from "mermaid";
import DOMPurify from "dompurify";
import { useTheme } from "@/hooks/useTheme";

function sanitizeSvg(svg: string): string {
  return DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ["script", "foreignObject"],
    FORBID_ATTR: ["onerror", "onload", "onclick"],
  });
}

let mermaidInitPromise: Promise<void> | null = null;
let mermaidInitTheme: "light" | "dark" | null = null;

function ensureMermaidInit(theme: "light" | "dark", config: Parameters<typeof mermaid.initialize>[0]) {
  if (!mermaidInitPromise || mermaidInitTheme !== theme) {
    mermaidInitTheme = theme;
    mermaidInitPromise = (async () => {
      mermaid.initialize(config);
    })();
  }
  return mermaidInitPromise;
}

function mermaidThemeConfig(theme: "light" | "dark") {
  if (theme === "dark") {
    return {
      startOnLoad: false,
      securityLevel: "strict" as const,
      theme: "base" as const,
      themeVariables: {
        background: "transparent",
        primaryColor: "#1f2937",
        primaryTextColor: "#e5e7eb",
        primaryBorderColor: "#4b5563",
        lineColor: "#6b7280",
        tertiaryColor: "#111827",
      },
    };
  }

  return {
    startOnLoad: false,
    securityLevel: "strict" as const,
    theme: "base" as const,
    themeVariables: {
      background: "transparent",
      primaryColor: "#e2e8f0",
      primaryTextColor: "#334155",
      primaryBorderColor: "#94a3b8",
      lineColor: "#94a3b8",
      tertiaryColor: "#f1f5f9",
    },
  };
}

interface MermaidBlockProps {
  chart: string;
}

function sanitizeMermaid(raw: string): string {
  let chart = raw.trim();

  // Fix edge labels: A -- label --> B  →  A -->|label| B
  chart = chart.replace(/(\w+)\s+--\s+([^-\n][^>\n]*?)\s+-->\s+(\w+)/g, "$1 -->|$2| $3");
  chart = chart.replace(/(\w+)\s+--\s+([^-\n][^-\n]*?)\s+---\s+(\w+)/g, "$1 ---|$2| $3");

  // Remove parentheses inside square brackets: [foo(bar)baz] → [foobarbaz]
  chart = chart.replace(/\[([^\]]*)\(([^)]*)\)([^\]]*)\]/g, (_, pre, inner, post) => `[${pre}${inner}${post}]`);

  // Remove special characters that break mermaid: quotes, semicolons, backticks inside labels
  chart = chart.replace(/\[([^\]]*)\]/g, (_match, label: string) => {
    const clean = label.replace(/["`';]/g, "'").replace(/[<>]/g, "");
    return `[${clean}]`;
  });

  // Strip colon-style edge labels: A --> B: label  →  A --> B
  chart = chart.replace(/(-->)\s+(\w+(?:\[[^\]]*\])?)\s*:\s*[^\n]+/g, "$1 $2");
  // Keep pipe-style labels intact: A -->|label| B is valid mermaid syntax

  // Fix "graph TD;" → "graph TD" (trailing semicolons)
  chart = chart.replace(/^(graph\s+\w+)\s*;/gm, "$1");
  chart = chart.replace(/^(flowchart\s+\w+)\s*;/gm, "$1");

  // Remove style/classDef/class lines that often cause parse errors
  chart = chart.split("\n").filter(line => {
    const t = line.trim();
    return !t.startsWith("style ") && !t.startsWith("classDef ") && !t.startsWith("class ");
  }).join("\n");

  // Ensure the chart starts with a valid directive
  const firstLine = chart.split("\n")[0]?.trim() || "";
  if (!firstLine.startsWith("graph") && !firstLine.startsWith("flowchart") &&
      !firstLine.startsWith("sequenceDiagram") && !firstLine.startsWith("pie") &&
      !firstLine.startsWith("gantt") && !firstLine.startsWith("erDiagram") &&
      !firstLine.startsWith("%%")) {
    chart = "graph TD\n" + chart;
  }

  return chart;
}

function simplifyMermaid(chart: string): string {
  const lines = chart.split("\n");
  const simplified: string[] = [];
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith("graph ") || trimmed.startsWith("flowchart ")) { simplified.push(trimmed); continue; }
    if (trimmed.startsWith("subgraph") || trimmed === "end" || trimmed.startsWith("style ") || trimmed.startsWith("classDef ")) continue;
    if (trimmed.match(/^\w+\s*-->/) || trimmed.match(/^\w+\s*---/)) {
      simplified.push(`    ${trimmed.replace(/-->?\|[^|]*\|/g, "-->").replace(/---?\|[^|]*\|/g, "---")}`);
    }
  }
  return simplified.join("\n") || "graph TD\n    A[No diagram available]";
}

export function MermaidBlock({ chart }: MermaidBlockProps) {
  const { resolvedTheme } = useTheme();
  const [error, setError] = useState<string | null>(null);
  const [svg, setSvg] = useState<string>("");
  const [expanded, setExpanded] = useState(false);
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    const render = async () => {
      const theme = resolvedTheme === "dark" ? "dark" : "light";
      await ensureMermaidInit(theme, mermaidThemeConfig(theme));
      const sanitized = sanitizeMermaid(chart);
      // Mermaid v10+ resolves the promise even on parse errors, but returns a
      // diagnostic SVG containing the error text. Detect these cases explicitly.
      const isErrorSvg = (svg: string) =>
        svg.includes("Syntax error in text") || svg.includes("class=\"error-icon\"");
      // Mermaid IDs must start with a letter; Math.random().toString(36).slice(2)
      // can begin with a digit, which breaks selector lookups inside mermaid.
      const newId = () => `m${Math.random().toString(36).slice(2).replace(/[^a-zA-Z0-9]/g, "")}`;

      try {
        const id = newId();
        const result = await mermaid.render(id, sanitized);
        if (isErrorSvg(result.svg)) throw new Error("mermaid returned error svg");
        setSvg(result.svg);
        setError(null);
      } catch {
        try {
          const id2 = newId();
          const result2 = await mermaid.render(id2, simplifyMermaid(sanitized));
          if (isErrorSvg(result2.svg)) throw new Error("mermaid returned error svg after simplify");
          setSvg(result2.svg);
          setError(null);
        } catch (err2) {
          setError(String(err2));
        }
      }
    };
    render();
  }, [chart, resolvedTheme]);

  const handleZoomIn = useCallback(() => setZoom(z => Math.min(z + 0.25, 3)), []);
  const handleZoomOut = useCallback(() => setZoom(z => Math.max(z - 0.25, 0.5)), []);
  const handleReset = useCallback(() => setZoom(1), []);

  if (error) {
    return (
      <details className="my-4 rounded-lg border border-muted bg-muted/30 p-3">
        <summary className="text-xs text-muted-foreground cursor-pointer">Diagram could not be rendered — click to view source</summary>
        <pre className="mt-2 text-xs text-muted-foreground overflow-auto whitespace-pre-wrap">{chart}</pre>
      </details>
    );
  }

  // For inline view: let SVG render at natural size, scroll if wider than container
  // Extract original width to decide scaling strategy
  const widthMatch = svg.match(/width="([\d.]+)"/);
  const origWidth = widthMatch ? parseFloat(widthMatch[1]) : 0;

  // If diagram is very wide (>800px), scale it down but keep readable
  const inlineSvgRaw = origWidth > 800
    ? svg.replace(/<svg /, '<svg style="height:auto;min-height:250px" ')
    : svg.replace(/<svg /, '<svg style="width:100%;height:auto;min-height:200px" ');
  const inlineSvg = sanitizeSvg(inlineSvgRaw);

  // For expanded view: force SVG to fill available width
  const expandedSvgRaw = svg
    .replace(/width="[\d.]+"/, 'width="100%"')
    .replace(/height="[\d.]+"/, 'height="100%"')
    .replace(/<svg /, '<svg style="width:100%;height:auto;min-width:80vw" ');
  const expandedSvg = sanitizeSvg(expandedSvgRaw);

  if (expanded) {
    return (
      <div className="fixed inset-0 z-[100] bg-background/95 backdrop-blur-md" onClick={() => setExpanded(false)}>
        {/* Toolbar */}
        <div className="absolute top-4 left-1/2 -translate-x-1/2 z-10 flex items-center gap-1 rounded-lg bg-card border border-border p-1 shadow-lg" onClick={e => e.stopPropagation()}>
          <button onClick={handleZoomOut} className="p-2 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors" title="Zoom out">
            <ZoomOut className="h-4 w-4" />
          </button>
          <span className="text-xs text-muted-foreground px-2 min-w-[3rem] text-center">{Math.round(zoom * 100)}%</span>
          <button onClick={handleZoomIn} className="p-2 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors" title="Zoom in">
            <ZoomIn className="h-4 w-4" />
          </button>
          <button onClick={handleReset} className="p-2 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors" title="Reset zoom">
            <RotateCcw className="h-4 w-4" />
          </button>
          <div className="w-px h-5 bg-border mx-1" />
          <button onClick={() => setExpanded(false)} className="p-2 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors" title="Close">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Diagram — centered, scrollable when zoomed */}
        <div className="absolute inset-0 overflow-auto flex items-center justify-center pt-14 pb-8 px-8" onClick={e => e.stopPropagation()}>
          <div
            className="transition-transform duration-200"
            style={{ transform: `scale(${zoom})`, transformOrigin: "center center", minWidth: "70vw" }}
            dangerouslySetInnerHTML={{ __html: expandedSvg }}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="group my-6">
      <div className="relative rounded-lg bg-muted/20 border border-border p-6 overflow-x-auto cursor-pointer min-h-[200px] flex items-center" onClick={() => setExpanded(true)}>
        {/* Expand hint */}
        <div className="absolute top-2 right-2 flex items-center gap-1.5 rounded-md bg-background/70 border border-border px-2 py-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <Maximize2 className="h-3 w-3 text-muted-foreground" />
          <span className="text-[10px] text-muted-foreground">Click to enlarge</span>
        </div>
        {/* SVG — responsive, scales to container */}
        <div className="w-full" dangerouslySetInnerHTML={{ __html: inlineSvg }} />
      </div>
    </div>
  );
}
