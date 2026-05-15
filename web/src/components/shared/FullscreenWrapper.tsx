import { useEffect, useState, type ReactNode } from "react";
import { Maximize2, Minimize2 } from "lucide-react";

interface Props {
  children: ReactNode;
  /** Optional aria-label override for the enlarge button. */
  label?: string;
  /** Optional className applied to the inline (non-fullscreen) wrapper. */
  className?: string;
  /** Where to place the Enlarge button when not fullscreen. */
  buttonPlacement?: "overlay" | "inline";
  /** Horizontal alignment for the non-fullscreen control. */
  buttonAlign?: "left" | "right";
}

/**
 * Wraps a child surface (graph canvas, etc.) with an Enlarge → Esc
 * fullscreen affordance. Built on a plain ``fixed inset-0`` overlay
 * with full-opacity ``bg-background`` rather than the ``MediaModal``
 * pattern — MediaModal closes on click-outside, which would dismiss
 * the graph on every node click.
 *
 * The wrapper does NOT manage cytoscape's resize() — children that
 * need a resize tick on enter/exit fullscreen should observe their
 * own container via ResizeObserver (cytoscape already does this in
 * GraphCanvas / WikiGraph).
 */
export function FullscreenWrapper({
  children,
  label = "Enlarge",
  className,
  buttonPlacement = "inline",
  buttonAlign = "right",
}: Props) {
  const [isFullscreen, setIsFullscreen] = useState(false);

  useEffect(() => {
    if (!isFullscreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setIsFullscreen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isFullscreen]);

  // Lock body scroll while fullscreen so background scroll doesn't
  // shift behind the overlay.
  useEffect(() => {
    if (!isFullscreen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [isFullscreen]);

  if (isFullscreen) {
    return (
      <div
        className="fixed inset-0 z-50 bg-background"
        role="dialog"
        aria-modal="true"
        aria-label={`${label} (fullscreen)`}
      >
        <div className="relative h-full w-full">
          {children}
          <div className="absolute right-4 top-4 z-10 flex items-center gap-2">
            <span className="rounded-md bg-card/90 border border-border px-2 py-1 text-[11px] text-muted-foreground shadow-sm">
              Esc to exit
            </span>
            <button
              type="button"
              onClick={() => setIsFullscreen(false)}
              aria-label="Exit fullscreen"
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card/90 px-2.5 py-1.5 text-xs font-medium hover:bg-muted transition-colors shadow-sm"
            >
              <Minimize2 size={12} />
              Minimize
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (buttonPlacement === "inline") {
    return (
      <div className={`flex h-full w-full min-h-0 flex-col ${className ?? ""}`}>
        <div className={`flex px-4 py-2 ${buttonAlign === "left" ? "justify-start" : "justify-end"}`}>
          <button
            type="button"
            onClick={() => setIsFullscreen(true)}
            aria-label={label}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card/90 px-2.5 py-1.5 text-xs font-medium hover:bg-muted transition-colors shadow-sm"
          >
            <Maximize2 size={12} />
            {label}
          </button>
        </div>
        <div className="min-h-0 flex-1">{children}</div>
      </div>
    );
  }

  return (
    <div className={`relative h-full w-full ${className ?? ""}`}>
      {children}
      <button
        type="button"
        onClick={() => setIsFullscreen(true)}
        aria-label={label}
        className={`absolute top-4 z-10 inline-flex items-center gap-1.5 rounded-md border border-border bg-card/90 px-2.5 py-1.5 text-xs font-medium hover:bg-muted transition-colors shadow-sm ${buttonAlign === "left" ? "left-4" : "right-4"}`}
      >
        <Maximize2 size={12} />
        {label}
      </button>
    </div>
  );
}
