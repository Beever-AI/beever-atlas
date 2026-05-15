/**
 * ViewExplainerButton — small `?` info icon that opens a modal
 * explaining what each option of a sub-navigation toggle means.
 *
 * Pattern: park the button immediately to the right of a
 * SegmentedToggle. Click → centered overlay modal with a colored chip
 * per concept and a one-paragraph plain-English description. Operators
 * who already know what the views are can ignore the icon entirely;
 * operators new to the surface get full context with one click instead
 * of guessing.
 *
 * Esc closes. Backdrop click closes. The trigger is a 28px square so
 * it sits inline with most toggle button heights without crowding.
 */
import { useEffect, useState, type ReactNode } from "react";
import { HelpCircle, X } from "lucide-react";

export interface ExplainerSection {
  /** Display title (matches the toggle option label, e.g. "3-Tier Memory") */
  title: string;
  /** Lucide icon for visual recognition */
  icon: React.ComponentType<{ className?: string; size?: number }>;
  /** Tailwind tint applied to the icon chip background — should match
   *  whatever color identity the surface uses in the rest of the UI. */
  accent: string;
  /** Short one-line tagline shown under the title */
  tagline?: string;
  /** Rich body content — use ReactNode so callers can pass JSX with
   *  sub-headings, lists, etc. Stays inside the modal scroll area. */
  body: ReactNode;
}

interface ViewExplainerButtonProps {
  heading: string;
  sections: ExplainerSection[];
  /** Optional aria-label override for the trigger button */
  triggerLabel?: string;
}

export function ViewExplainerButton({
  heading,
  sections,
  triggerLabel = "What does each view mean?",
}: ViewExplainerButtonProps) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={triggerLabel}
        title={triggerLabel}
        className="inline-flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground/70 hover:bg-muted hover:text-foreground transition-colors"
      >
        <HelpCircle size={15} />
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm animate-in fade-in duration-150"
          role="dialog"
          aria-modal="true"
          aria-label={heading}
          onClick={() => setOpen(false)}
        >
          <div
            className="w-[min(560px,calc(100vw-2rem))] max-h-[85vh] overflow-y-auto rounded-2xl border border-border bg-card p-6 shadow-2xl animate-in zoom-in-95 duration-200"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-4 mb-5">
              <h2 className="text-lg font-semibold text-foreground leading-tight">
                {heading}
              </h2>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close"
                className="shrink-0 inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              >
                <X size={15} />
              </button>
            </div>

            <div className="space-y-5">
              {sections.map((s, idx) => (
                <div key={s.title}>
                  {idx > 0 && (
                    <div className="h-px bg-border/60 mb-5" />
                  )}
                  <div className="flex items-start gap-3">
                    <span
                      className={`shrink-0 mt-0.5 flex h-9 w-9 items-center justify-center rounded-xl ${s.accent}`}
                    >
                      <s.icon size={18} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <h3 className="text-base font-semibold text-foreground leading-tight">
                        {s.title}
                      </h3>
                      {s.tagline && (
                        <p className="text-[13px] text-muted-foreground mt-0.5">
                          {s.tagline}
                        </p>
                      )}
                      <div className="mt-3 text-sm text-foreground/85 leading-relaxed space-y-2.5">
                        {s.body}
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export default ViewExplainerButton;
