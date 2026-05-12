import { KeyRound, Plus } from "lucide-react";
import { PRESET_LABELS } from "@/lib/aiSetup";

interface EndpointsEmptyStateProps {
  /** Open the inline Add-Endpoint panel. */
  onAdd: () => void;
  /** Apply a quick-start assignment preset by key (from ``PRESET_LABELS``). */
  onApplyPreset: (presetKey: string) => void;
  /** Disable the controls while a request is in flight. */
  busy?: boolean;
}

const PRESET_CHIPS = Object.entries(PRESET_LABELS).filter(([key]) => key !== "custom");

/**
 * Dashed-border CTA shown when there are zero endpoints — mirrors the
 * Integrations-tab empty state. Offers (a) a big "Add endpoint" button and
 * (b) quick-start preset chips wired to ``asn.applyPreset`` (which may fail
 * with ``preset_requirements_not_met`` — the parent shows the explanatory
 * message + Add button in that case).
 */
export function EndpointsEmptyState({ onAdd, onApplyPreset, busy = false }: EndpointsEmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-14 px-6 rounded-2xl border-2 border-dashed border-border bg-card text-center">
      <div className="w-12 h-12 rounded-xl bg-muted/60 flex items-center justify-center text-muted-foreground mb-4">
        <KeyRound className="w-6 h-6" />
      </div>
      <h2 className="text-lg font-semibold text-foreground mb-1">No endpoints yet</h2>
      <p className="text-sm text-muted-foreground max-w-md mb-5">
        Add one to start, or apply a quick-start preset to set everything up at once.
      </p>
      <button
        type="button"
        onClick={onAdd}
        disabled={busy}
        className="inline-flex items-center gap-1.5 px-5 py-2.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 disabled:opacity-50 transition-colors"
      >
        <Plus className="w-4 h-4" />
        Add endpoint
      </button>
      <div className="mt-5 flex flex-wrap items-center justify-center gap-1.5">
        <span className="text-[11px] text-muted-foreground">…or apply a preset:</span>
        {PRESET_CHIPS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => onApplyPreset(key)}
            disabled={busy}
            className="rounded-md border border-border px-2.5 py-1 text-xs font-medium hover:bg-accent disabled:opacity-50"
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}
