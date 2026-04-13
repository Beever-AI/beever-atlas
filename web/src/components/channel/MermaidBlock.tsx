import { useEffect, useId, useRef, useState } from "react";

interface MermaidBlockProps {
  code: string;
}

let mermaidInitialized = false;

/**
 * Lazy-loads mermaid and renders a diagram SVG.
 * Falls back to a styled <pre> block on parse/render errors.
 * Mermaid is only imported when this component actually mounts,
 * keeping it out of the main bundle when no mermaid blocks are present.
 */
export function MermaidBlock({ code }: MermaidBlockProps) {
  const reactId = useId();
  // mermaid requires IDs that start with a letter and contain no colons
  const diagramId = `mermaid-${reactId.replace(/[^a-zA-Z0-9-]/g, "")}`;

  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [svg, setSvg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function render() {
      try {
        const mermaid = (await import("mermaid")).default;

        if (!mermaidInitialized) {
          mermaid.initialize({
            startOnLoad: false,
            theme: "default",
            securityLevel: "strict",
          });
          mermaidInitialized = true;
        }

        // Validate first so bad syntax throws instead of producing an
        // error-SVG that slips past the catch branch. Mermaid v10+ may
        // return a valid SVG containing "Syntax error" rather than throw.
        await mermaid.parse(code, { suppressErrors: false });
        const { svg: rendered } = await mermaid.render(diagramId, code);
        // Belt-and-braces: reject SVGs that mermaid emitted as an error
        // banner (some versions swallow the throw inside render()).
        if (rendered.includes("Syntax error in text") || rendered.includes("mermaid version")) {
          throw new Error("Mermaid render produced a syntax-error SVG.");
        }
        if (!cancelled) {
          setSvg(rendered);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
          setSvg(null);
        }
      }
    }

    render();
    return () => {
      cancelled = true;
    };
  }, [code, diagramId]);

  if (error !== null) {
    return (
      <div className="mb-3">
        <p className="text-xs text-muted-foreground mb-1">Could not render diagram.</p>
        <pre className="block p-3 bg-muted rounded-lg text-xs overflow-x-auto whitespace-pre-wrap">
          {code}
        </pre>
      </div>
    );
  }

  if (svg !== null) {
    return (
      <div
        ref={containerRef}
        className="mb-3 overflow-x-auto"
        // biome-ignore lint/security/noDangerouslySetInnerHtml: mermaid generates trusted SVG
        dangerouslySetInnerHTML={{ __html: svg }}
      />
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
