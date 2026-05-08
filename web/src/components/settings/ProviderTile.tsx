import { Check, Globe, HardDrive, Languages } from "lucide-react";
import {
  PROVIDER_DEFAULT_MODEL,
  PROVIDER_DESCRIPTIONS,
  PROVIDER_LABELS,
  formatCost,
  lookupModel,
} from "@/lib/knownEmbeddingModels";
import type { EmbeddingProvider } from "@/lib/types";

interface ProviderTileProps {
  provider: EmbeddingProvider;
  selected: boolean;
  onClick: () => void;
}

export function ProviderTile({ provider, selected, onClick }: ProviderTileProps) {
  const defaultModel = PROVIDER_DEFAULT_MODEL[provider];
  const spec = defaultModel ? lookupModel(provider, defaultModel) : null;
  const muted = !defaultModel || !spec;

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={muted}
      className={[
        "relative text-left rounded-lg border p-3 transition-all",
        "focus:outline-none focus:ring-2 focus:ring-primary/30",
        selected
          ? "border-primary bg-primary/5 ring-1 ring-primary/20 shadow-sm"
          : "border-border hover:border-primary/50 hover:bg-muted/30",
        muted ? "opacity-50 cursor-not-allowed" : "cursor-pointer",
      ].join(" ")}
    >
      {selected && (
        <div className="absolute top-2 right-2 w-5 h-5 rounded-full bg-primary text-primary-foreground flex items-center justify-center">
          <Check className="w-3 h-3" />
        </div>
      )}
      <div className="text-sm font-semibold text-foreground pr-6">
        {PROVIDER_LABELS[provider]}
      </div>
      {defaultModel && (
        <div className="text-[11px] text-muted-foreground font-mono mt-0.5 truncate">
          {defaultModel}
        </div>
      )}
      {spec && (
        <div className="mt-2 text-[11px] text-muted-foreground">
          {spec.dim}d
        </div>
      )}
      <div className="text-[11px] text-muted-foreground mt-1.5 line-clamp-2">
        {PROVIDER_DESCRIPTIONS[provider]}
      </div>
      {spec && (
        <div className="flex flex-wrap gap-1 mt-2">
          <Pill
            icon={<Languages className="w-2.5 h-2.5" />}
            label={spec.multilingual ? "Multilingual" : "English"}
            variant={spec.multilingual ? "ok" : "warn"}
          />
          <Pill label={formatCost(spec)} variant="muted" />
          <Pill
            icon={spec.local ? <HardDrive className="w-2.5 h-2.5" /> : <Globe className="w-2.5 h-2.5" />}
            label={spec.local ? "Local" : "Cloud"}
            variant={spec.local ? "info" : "muted"}
          />
        </div>
      )}
      {muted && (
        <div className="text-[10px] text-muted-foreground italic mt-2">
          Configurable via env, no UI preset yet
        </div>
      )}
    </button>
  );
}

function Pill({
  icon,
  label,
  variant,
}: {
  icon?: React.ReactNode;
  label: string;
  variant: "ok" | "warn" | "info" | "muted";
}) {
  const styles = {
    ok: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30",
    warn: "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/30",
    info: "bg-sky-500/10 text-sky-600 dark:text-sky-400 border-sky-500/30",
    muted: "bg-muted text-muted-foreground border-border",
  }[variant];
  return (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[10px] ${styles}`}
    >
      {icon}
      {label}
    </span>
  );
}
