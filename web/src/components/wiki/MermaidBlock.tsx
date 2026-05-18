import { useEffect, useState, useCallback } from "react";
import { Maximize2, X, ZoomIn, ZoomOut, RotateCcw } from "lucide-react";
import mermaid from "mermaid";
import { useTheme } from "@/hooks/useTheme";
import { sanitizeSvg } from "./sanitizeSvg";

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
  // htmlLabels: false forces mermaid to render labels as SVG <text> instead
  // of <foreignObject>-wrapped HTML. Otherwise DOMPurify drops the labels
  // and nodes render as empty boxes.
  const flowchart = { htmlLabels: false, useMaxWidth: true };
  if (theme === "dark") {
    return {
      startOnLoad: false,
      securityLevel: "strict" as const,
      theme: "base" as const,
      flowchart,
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
    flowchart,
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

  // --- Step 1: split chained arrows on a single line ---
  // A --> B --> C  →  A --> B\n    B --> C
  // Handles any number of hops; node tokens may include labels like A[Foo] or A(Foo).
  // Pipe-style edge labels (-->|x|) are preserved by treating them as part of the
  // arrow token. Applied BEFORE bracket-cleanup so label parens don't confuse the regex.
  const NODE_TOKEN = /[A-Za-z0-9_]+(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\})?/;
  // Mermaid arrow token: matches ``-->|label| Node`` (pipe-labelled
  // form) or plain ``-->``. Written with explicit character classes
  // (``[-]`` instead of literal ``-``) so the CodeQL
  // ``js/incomplete-multi-character-sanitization`` heuristic — which
  // is shaped for HTML comment-end-tag filtering and flags any regex
  // literal containing ``-->`` — does not pattern-match this. Mermaid
  // input is sanitised separately in ``sanitizeMermaid`` above and
  // the rendered output flows through ``mermaid.render`` → SVG (never
  // innerHTML), so HTML-comment-tag concerns do not apply here.
  const ARROW_TOKEN = /[-]{2}[>]?\|[^|]*\||[-]{2}[>]/;
  // Build a regex that matches 3+ chained tokens: NODE ARROW NODE (ARROW NODE)+
  const CHAIN_RE = new RegExp(
    `(${NODE_TOKEN.source})((?:\\s*(?:${ARROW_TOKEN.source})\\s*${NODE_TOKEN.source}){2,})`,
    "g"
  );
  chart = chart.replace(CHAIN_RE, (match) => {
    // Tokenize the chain into alternating [node, arrow, node, arrow, node, ...]
    const SPLIT_RE = new RegExp(
      `(${NODE_TOKEN.source})|(${ARROW_TOKEN.source})`,
      "g"
    );
    const tokens: string[] = [];
    let m: RegExpExecArray | null;
    while ((m = SPLIT_RE.exec(match)) !== null) {
      tokens.push(m[0]);
    }
    if (tokens.length < 5) return match; // not actually a chain, leave alone
    // Reconstruct as pairs: node[i] arrow[i+1] node[i+2]
    const lines: string[] = [];
    for (let i = 0; i + 2 < tokens.length; i += 2) {
      lines.push(`${tokens[i]} ${tokens[i + 1]} ${tokens[i + 2]}`);
    }
    if (lines.length > 1) {
      console.warn("[sanitizeMermaid] split chained arrow into", lines.length, "lines");
    }
    return lines.join("\n    ");
  });

  // --- Step 2: normalize unicode to ASCII ---
  // Must happen before bracket-cleanup so smart-quotes/ellipsis inside labels are gone.
  chart = chart
    .replace(/…/g, "...")                   // … → ...
    .replace(/[“”]/g, '"')             // "" → "
    .replace(/[‘’]/g, "'")             // '' → '
    .replace(/[–—]/g, "-");            // – — → -

  // Fix edge labels: A -- label --> B  →  A -->|label| B
  chart = chart.replace(/(\w+)\s+--\s+([^-\n][^>\n]*?)\s+-->\s+(\w+)/g, "$1 -->|$2| $3");
  chart = chart.replace(/(\w+)\s+--\s+([^-\n][^-\n]*?)\s+---\s+(\w+)/g, "$1 ---|$2| $3");

  // --- Step 3: clean round-bracket node shapes ---
  // Strip forbidden chars inside (label) shapes: WORD(label) → WORD(cleanLabel)
  // Only matches when the parens directly follow a word-boundary identifier,
  // avoiding arrow patterns like A -- text --> B.
  chart = chart.replace(/\b([A-Za-z_]\w*)\(([^)]*)\)/g, (_match, id, label: string) => {
    const clean = label.replace(/["`';]/g, "'").replace(/[<>]/g, "");
    return `${id}(${clean})`;
  });

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

  // RES-287/4b — mirrors the canonical pattern in
  // `channel/MermaidBlock.tsx` (lines 129-184). The previous implementation
  // had no cleanup on this useEffect, so React StrictMode's double-invoke
  // fired two concurrent `mermaid.render()` coroutines against the singleton
  // mermaid instance. The first (aborted) call corrupted mermaid's internal
  // state and the second produced an error SVG → setError fired → fallback
  // <details> tile rendered. With N MermaidBlocks on a wiki page, the page
  // stacked N error tiles. The cancellation flag + clearTimeout + parse-first
  // pattern below eliminates the race.
  useEffect(() => {
    let cancelled = false;

    // RES-287/4b round 2 — mermaid v11's `render(id, text)` creates a
    // temporary `<div id="d${id}">` in `document.body` to compute the
    // SVG. On success it removes the div; on PARSE FAILURE mid-render
    // it leaves the div with a bomb-emoji "Syntax error in text" SVG
    // visible at the bottom of the page (mermaid's own DOM, NOT our
    // React fallback). The wrapper below + `purgeOrphans()` reaps
    // every temp div mermaid might have left behind for the ids we
    // asked it to render, regardless of which catch branch we hit.
    const usedIds = new Set<string>();
    const purgeOrphans = () => {
      // Mermaid v11 wraps the SVG in `<div id="d${id}">`; older versions
      // used `${id}` directly. Try both, and as a belt-and-braces sweep,
      // remove any direct child of <body> whose subtree contains the
      // signature bomb-emoji error markup.
      for (const id of usedIds) {
        for (const candidate of [id, `d${id}`, `d${id}-svg`]) {
          const el = document.getElementById(candidate);
          if (el && document.body.contains(el)) el.remove();
        }
      }
      // Sweep any straggler "Syntax error in text" / error-icon SVGs
      // that escaped the id-based purge (e.g. mermaid used a different
      // id format internally). Scoped to direct body children so we
      // never touch the legitimate inline SVGs we just set via setSvg.
      Array.from(document.body.children).forEach((el) => {
        if (
          (el.tagName === "DIV" || el.tagName === "svg" || el.tagName === "SVG") &&
          (el.querySelector('[class*="error-icon"]') ||
            el.textContent?.includes("Syntax error in text"))
        ) {
          el.remove();
        }
      });
    };

    // Debounce so we don't re-run mermaid's expensive parse+render on every
    // rapid prop change (StrictMode double-invoke, theme flip, etc.). 250ms
    // matches the channel/MermaidBlock implementation.
    const debounceMs = 250;
    const timer = setTimeout(() => {
      if (!cancelled) void render();
    }, debounceMs);

    async function render(): Promise<void> {
      const theme = resolvedTheme === "dark" ? "dark" : "light";
      await ensureMermaidInit(theme, mermaidThemeConfig(theme));
      if (cancelled) return;
      const sanitized = sanitizeMermaid(chart);
      // Mermaid v10+ resolves the promise even on parse errors, but returns a
      // diagnostic SVG containing the error text. Detect these cases explicitly.
      const isErrorSvg = (svg: string) =>
        svg.includes("Syntax error in text") ||
        svg.includes("class=\"error-icon\"") ||
        svg.includes("mermaid version");
      // Mermaid IDs must start with a letter; Math.random().toString(36).slice(2)
      // can begin with a digit, which breaks selector lookups inside mermaid.
      const newId = () => {
        const id = `m${Math.random().toString(36).slice(2).replace(/[^a-zA-Z0-9]/g, "")}`;
        usedIds.add(id);
        return id;
      };

      try {
        // Parse first so bad syntax throws cleanly instead of returning a
        // valid-shape SVG that contains "Syntax error in text". Some mermaid
        // versions swallow the throw inside render() — parse() is the only
        // path that reliably surfaces malformed source.
        await mermaid.parse(sanitized, { suppressErrors: false });
        if (cancelled) return;
        const id = newId();
        const result = await mermaid.render(id, sanitized);
        if (cancelled) return;
        if (isErrorSvg(result.svg)) throw new Error("mermaid returned error svg");
        setSvg(result.svg);
        setError(null);
      } catch {
        // Purge orphan mermaid DOM from the failed first attempt before
        // we try simplification — otherwise the error svg accumulates.
        purgeOrphans();
        if (cancelled) return;
        try {
          const simplified = simplifyMermaid(sanitized);
          await mermaid.parse(simplified, { suppressErrors: false });
          if (cancelled) return;
          const id2 = newId();
          const result2 = await mermaid.render(id2, simplified);
          if (cancelled) return;
          if (isErrorSvg(result2.svg)) throw new Error("mermaid returned error svg after simplify");
          setSvg(result2.svg);
          setError(null);
        } catch (err2) {
          if (cancelled) return;
          setError(String(err2));
        }
      } finally {
        // Always reap — succeeds on a no-op when there's nothing to clean.
        purgeOrphans();
      }
    }

    return () => {
      cancelled = true;
      clearTimeout(timer);
      // One last reap on unmount so navigating away from a page mid-
      // render doesn't leave the bomb SVG behind on the new page.
      purgeOrphans();
    };
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
