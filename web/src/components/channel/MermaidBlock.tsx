import { useEffect, useId, useRef, useState } from "react";
import { Maximize2, X } from "lucide-react";
import { sanitizeSvg } from "@/components/wiki/sanitizeSvg";

interface MermaidBlockProps {
  code: string;
}

let mermaidInitPromise: Promise<void> | null = null;

// LLMs frequently emit node labels that contain characters mermaid treats as
// syntax: `(`, `)`, `,`, `&`, `/`, `:`. Wrapping such labels in quotes is the
// documented escape hatch. Quote them automatically when not already quoted,
// skipping directive/style lines. Also strip stray `&` used as text between
// nodes (LLMs sometimes write `A --> X & Y --> B` meaning prose, not mermaid's
// multi-edge operator).
function preprocessMermaid(code: string): string {
  const NEEDS_QUOTE = /[(),&/:]/;
  const quoteLabel = (open: string, close: string, inner: string) => {
    const trimmed = inner.trim();
    if (!trimmed || trimmed.startsWith('"')) return `${open}${inner}${close}`;
    if (!NEEDS_QUOTE.test(trimmed)) return `${open}${inner}${close}`;
    const escaped = trimmed.replace(/"/g, "#quot;");
    return `${open}"${escaped}"${close}`;
  };

  return code
    .split("\n")
    .map((line) => {
      if (/^\s*(style|classDef|class |click|linkStyle|subgraph|end\b|%%|---|direction\b)/.test(line)) {
        return line;
      }
      // Cylinder [( ... )]
      let out = line.replace(/\[\(([^\]]+?)\)\]/g, (_m, inner) => quoteLabel("[(", ")]", inner));
      // Subroutine [[ ... ]]
      out = out.replace(/\[\[([^\]]+?)\]\]/g, (_m, inner) => quoteLabel("[[", "]]", inner));
      // Hexagon {{ ... }}
      out = out.replace(/\{\{([^}]+?)\}\}/g, (_m, inner) => quoteLabel("{{", "}}", inner));
      // Circle (( ... ))
      out = out.replace(/\(\(([^)]+?)\)\)/g, (_m, inner) => quoteLabel("((", "))", inner));
      // Rect [ ... ]
      out = out.replace(/\[([^\]\[]+?)\]/g, (m, inner) => {
        if (m.startsWith("[(") || m.startsWith("[[") || m.startsWith("[/") || m.startsWith("[\\")) return m;
        return quoteLabel("[", "]", inner);
      });
      // Round ( ... ) — only when it directly follows an identifier, to avoid
      // matching arrow labels like `-- text -->`.
      out = out.replace(/(\b[A-Za-z_][\w-]*)\(([^()]+?)\)/g, (_m, id, inner) =>
        `${id}${quoteLabel("(", ")", inner)}`
      );
      // Diamond { ... }
      out = out.replace(/(\b[A-Za-z_][\w-]*)\{([^{}]+?)\}/g, (_m, id, inner) =>
        `${id}${quoteLabel("{", "}", inner)}`
      );
      return out;
    })
    .join("\n");
}

// Last-resort pass for malformed LLM output: drop lines that are structurally
// invalid (dangling edges, style/class directives, subgraph blocks with bare
// node ids). Keeps the minimum viable graph so users still see *something*.
function simplifyMermaid(code: string): string {
  const lines = code.split("\n");
  const kept: string[] = [];
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    if (/^(graph|flowchart)\s+/i.test(line)) { kept.push(line); continue; }
    if (/^(style|classDef|class |click|linkStyle|subgraph|end\b|%%|direction\b)/i.test(line)) continue;
    // Dangling edge: arrow with no right-hand target.
    if (/-->\s*\|[^|]*\|\s*$/.test(line)) continue;
    if (/--\s*$/.test(line)) continue;
    // Must look like an edge or a node declaration.
    if (!/-->|---|==>|-\.-/.test(line) && !/\[|\(|\{/.test(line)) continue;
    kept.push(line);
  }
  if (!kept.some((l) => /^(graph|flowchart)\s+/i.test(l))) kept.unshift("flowchart TD");
  return kept.join("\n");
}

async function ensureMermaidInit() {
  if (!mermaidInitPromise) {
    mermaidInitPromise = (async () => {
      const mermaid = (await import("mermaid")).default;
      mermaid.initialize({
        startOnLoad: false,
        theme: "base",
        securityLevel: "strict",
        themeVariables: {
          background: "transparent",
          primaryColor: "#e2e8f0",
          primaryTextColor: "#0f172a",
          primaryBorderColor: "#94a3b8",
          lineColor: "#94a3b8",
          tertiaryColor: "#f1f5f9",
        },
        // useMaxWidth:false keeps the SVG at its natural size so dense
        // flowcharts stay readable; the parent container scrolls horizontally.
        flowchart: { htmlLabels: false, useMaxWidth: false },
      });
    })();
  }
  return mermaidInitPromise;
}

/**
 * Lazy-loads mermaid and renders a diagram SVG.
 * Falls back to a styled <pre> block on parse/render errors.
 * Mermaid is only imported when this component actually mounts,
 * keeping it out of the main bundle when no mermaid blocks are present.
 */
export function MermaidBlock({ code }: MermaidBlockProps) {
  const reactId = useId();
  // Mermaid IDs must start with a letter and contain only alphanumerics.
  // Strip everything else and prefix with "m" so a leading digit or dash
  // in the React-generated id can never produce an invalid selector.
  const diagramId = `m${reactId.replace(/[^a-zA-Z0-9]/g, "")}`;

  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [svg, setSvg] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;

    // Debounce so we don't re-run mermaid's expensive parse+render on every
    // streamed token while the LLM is still emitting the code block. Each
    // re-render also replaces the rendered SVG, which caused visible layout
    // thrash and scroll-jumping during streaming.
    const debounceMs = 250;
    const timer = setTimeout(() => {
      if (!cancelled) void render();
    }, debounceMs);

    async function render() {
      try {
        await ensureMermaidInit();
        const mermaid = (await import("mermaid")).default;

        // Validate first so bad syntax throws instead of producing an
        // error-SVG that slips past the catch branch. Mermaid v10+ may
        // return a valid SVG containing "Syntax error" rather than throw.
        const processed = preprocessMermaid(code);
        let source = processed;
        let rendered: string;
        try {
          await mermaid.parse(source, { suppressErrors: false });
          ({ svg: rendered } = await mermaid.render(diagramId, source));
        } catch {
          // First attempt failed; retry with a best-effort simplification so
          // malformed LLM output still produces *something* renderable.
          source = simplifyMermaid(processed);
          await mermaid.parse(source, { suppressErrors: false });
          ({ svg: rendered } = await mermaid.render(`${diagramId}s`, source));
        }
        // Belt-and-braces: reject SVGs that mermaid emitted as an error
        // banner (some versions swallow the throw inside render()).
        if (rendered.includes("Syntax error in text") || rendered.includes("mermaid version")) {
          throw new Error("Mermaid render produced a syntax-error SVG.");
        }
        if (!cancelled) {
          const cleanSvg = sanitizeSvg(rendered);
          setSvg(cleanSvg);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
          setSvg(null);
        }
      }
    }

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [code, diagramId]);

  if (error !== null) {
    return (
      <details className="mb-3 rounded-lg border border-border bg-muted/30 p-3">
        <summary className="text-xs text-muted-foreground cursor-pointer select-none">
          Diagram could not be rendered — click to view source
        </summary>
        <pre className="mt-2 text-[11px] text-muted-foreground overflow-x-auto whitespace-pre-wrap max-h-[260px]">
          {code}
        </pre>
      </details>
    );
  }

  if (svg !== null) {
    return (
      <>
        <div className="group relative mb-3 rounded-lg border border-border bg-muted/20">
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="absolute top-2 right-2 z-10 flex items-center gap-1.5 rounded-md bg-background/80 border border-border px-2 py-1 text-[11px] text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity hover:text-foreground"
            aria-label="Expand diagram"
          >
            <Maximize2 className="h-3 w-3" /> Expand
          </button>
          <div
            ref={containerRef}
            className="overflow-x-auto p-3 [&>svg]:max-h-[420px] [&>svg]:h-auto"
            // biome-ignore lint/security/noDangerouslySetInnerHtml: mermaid-generated, DOMPurify-sanitized SVG
            dangerouslySetInnerHTML={{ __html: svg }}
          />
        </div>
        {expanded && (
          <div
            className="fixed inset-0 z-[100] bg-background/95 backdrop-blur-md flex items-center justify-center p-8"
            onClick={() => setExpanded(false)}
          >
            <button
              type="button"
              onClick={() => setExpanded(false)}
              className="absolute top-4 right-4 p-2 rounded-md bg-card border border-border text-muted-foreground hover:text-foreground"
              aria-label="Close expanded diagram"
            >
              <X className="h-4 w-4" />
            </button>
            <div
              className="max-w-full max-h-full overflow-auto rounded-lg bg-muted/30 p-4"
              onClick={(e) => e.stopPropagation()}
              // biome-ignore lint/security/noDangerouslySetInnerHtml: mermaid-generated, DOMPurify-sanitized SVG
              dangerouslySetInnerHTML={{ __html: svg }}
            />
          </div>
        )}
      </>
    );
  }

  // Loading state — render placeholder with same height to avoid layout shift
  return (
    <div
      ref={containerRef}
      className="mb-3 min-h-[80px] bg-muted/40 rounded-lg animate-pulse"
      aria-label="Loading diagram"
    />
  );
}
