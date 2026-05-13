import { useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleDot,
  Loader2,
  Pencil,
  PlugZap,
  RefreshCw,
  Trash2,
} from "lucide-react";
import type { Endpoint } from "@/lib/aiSetup";
import { getPresetIdentity } from "@/lib/endpointPresetIdentity";

/** Inline result of a Test Connection call, surfaced on the card. */
export interface EndpointTestResult {
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
}

/** Inline result of a Discover Models call, surfaced on the card. */
export interface EndpointDiscoverResult {
  ok: boolean;
  count: number;
  error: string | null;
}

interface EndpointCardProps {
  endpoint: Endpoint;
  /** Number of assignments (primary + fallback) pointing at this endpoint. */
  usedByCount: number;
  /** Consumer names referencing this endpoint — shown in the "jobs" tooltip. */
  usedByConsumers?: string[];
  /** True while a Test / Discover / Delete / save on this endpoint is in flight. */
  busy?: boolean;
  /** Most recent test result (set by the parent after calling onTest). */
  testResult?: EndpointTestResult | null;
  /** Most recent discover result (set by the parent after calling onDiscover). */
  discoverResult?: EndpointDiscoverResult | null;
  /** When true the parent renders the inline editor in place of the body. */
  isEditing?: boolean;
  /** Render the inline editor (passed by the parent when ``isEditing``). */
  editor?: React.ReactNode;
  onEdit?: () => void;
  onTest: () => void;
  onDiscover: () => void;
  onDelete: () => void;
}

interface StatusPill {
  Icon: typeof CircleDot;
  /** Tailwind classes for the badge (bg + ring + text). */
  cls: string;
  label: string;
}

function computeStatus(e: Endpoint, testResult: EndpointTestResult | null | undefined): StatusPill {
  if (e.has_credential === false && e.auth_type !== "none") {
    return {
      Icon: CircleDot,
      cls: "bg-muted text-muted-foreground ring-border",
      label: "no key",
    };
  }
  if (testResult) {
    return testResult.ok
      ? {
          Icon: CheckCircle2,
          cls: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 ring-emerald-500/30",
          label: `connected · ${testResult.latency_ms}ms`,
        }
      : {
          Icon: AlertTriangle,
          cls: "bg-destructive/10 text-destructive ring-destructive/30",
          label: `failed: ${testResult.error ?? "unknown"}`,
        };
  }
  if (e.last_test_ok === true) {
    return {
      Icon: CheckCircle2,
      cls: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 ring-emerald-500/30",
      label: "tested ok",
    };
  }
  if (e.last_test_ok === false) {
    return {
      Icon: AlertTriangle,
      cls: "bg-destructive/10 text-destructive ring-destructive/30",
      label: e.last_test_error ?? "last test failed",
    };
  }
  return {
    Icon: CircleDot,
    cls: "bg-amber-500/10 text-amber-600 dark:text-amber-400 ring-amber-500/30",
    label: "untested",
  };
}

function Stat({ children, title }: { children: React.ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="inline-flex items-center rounded-md bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
    >
      {children}
    </span>
  );
}

/**
 * Presentational card for one Endpoint. The parent owns the ``useEndpoints``
 * hook and the edit/test/discover/delete handlers; this component just renders
 * state. Visual language tracks ``IntegrationsTab``'s ``PlatformCard`` — a
 * tinted, per-preset icon box; name + family chip; a rounded status badge
 * top-right; a labelled stat row + the base URL on its own ``font-mono`` line;
 * an expandable model-chip list; and an Edit / Test / Discover · Delete button
 * row. When ``isEditing`` the supplied ``editor`` replaces the body.
 */
export function EndpointCard({
  endpoint: e,
  usedByCount,
  usedByConsumers,
  busy = false,
  testResult,
  discoverResult,
  isEditing = false,
  editor,
  onEdit,
  onTest,
  onDiscover,
  onDelete,
}: EndpointCardProps) {
  const status = computeStatus(e, testResult);
  const identity = getPresetIdentity(e.preset);
  const { Icon } = identity;
  const [showModels, setShowModels] = useState(false);
  const modelCount = e.models.length;

  const jobsTitle =
    usedByCount === 0
      ? "Not used by any agent or the embedding model yet"
      : `In use by: ${usedByConsumers && usedByConsumers.length > 0 ? usedByConsumers.join(", ") : `${usedByCount} consumer${usedByCount === 1 ? "" : "s"}`}`;

  return (
    <div className="group rounded-xl border border-border bg-card overflow-hidden transition-shadow hover:shadow-md hover:shadow-black/5 dark:hover:shadow-black/20">
      {/* Header strip */}
      <div className="px-4 py-3 border-b border-border flex items-start gap-3">
        <div
          className={`w-10 h-10 rounded-lg flex items-center justify-center shrink-0 ${identity.iconBox}`}
        >
          <Icon className="w-5 h-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-foreground truncate">{e.name}</span>
            <span className={`inline-flex items-center rounded-md px-1.5 py-0.5 text-[10px] font-medium ${identity.chip}`}>
              {e.preset}
            </span>
          </div>
          {e.has_credential ? (
            <div className="text-xs text-muted-foreground font-mono truncate mt-0.5" title={e.credential_masked}>
              {e.credential_masked}
            </div>
          ) : (
            <div className="text-xs text-muted-foreground italic mt-0.5">
              {e.auth_type === "none" ? "no auth" : "no credential"}
            </div>
          )}
        </div>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 shrink-0 ${status.cls}`}
          title={status.label}
        >
          <status.Icon className="h-3 w-3" />
          <span className="max-w-[10rem] truncate">{status.label}</span>
        </span>
      </div>

      {/* Body — replaced by the editor when editing */}
      {isEditing ? (
        <div className="p-4">{editor}</div>
      ) : (
        <div className="px-4 py-3 space-y-3">
          {/* Stat row */}
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={() => modelCount > 0 && setShowModels((v) => !v)}
              disabled={modelCount === 0}
              className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-medium ${
                modelCount === 0
                  ? "bg-muted text-muted-foreground cursor-default opacity-70"
                  : "bg-muted text-muted-foreground hover:bg-muted/80"
              }`}
              aria-expanded={modelCount > 0 ? showModels : undefined}
              title={modelCount === 0 ? "No models — run Discover" : `${modelCount} models`}
            >
              {`${modelCount} models`}
              {modelCount > 0 &&
                (showModels ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />)}
            </button>
            <Stat title={jobsTitle}>{`${usedByCount} jobs`}</Stat>
            <Stat title={`${e.rpm} requests per minute`}>{`${e.rpm} RPM`}</Stat>
            {e.tags.length > 0 &&
              e.tags.map((t) => (
                <span
                  key={t}
                  className="inline-flex items-center rounded-md bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary"
                >
                  {`#${t}`}
                </span>
              ))}
          </div>

          {/* Base URL — own line, monospace, truncated */}
          <div className="text-xs font-mono text-muted-foreground truncate" title={e.base_url || "(no base url)"}>
            {e.base_url || "(no base url)"}
          </div>

          {/* Expandable model list */}
          {showModels && modelCount > 0 && (
            <div className="flex flex-wrap gap-1">
              {e.models.map((m) => (
                <span key={m} className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-xs font-mono text-muted-foreground">
                  {m}
                </span>
              ))}
            </div>
          )}
          {modelCount === 0 && (
            <div className="text-[11px] text-muted-foreground italic">(no models — run Discover)</div>
          )}

          {/* Extra headers — surfaced when present */}
          {Object.keys(e.headers).length > 0 && (
            <div className="flex flex-wrap gap-1">
              {Object.entries(e.headers).map(([k, v]) => (
                <span
                  key={k}
                  className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-[11px] font-mono text-muted-foreground"
                  title={`${k}: ${v}`}
                >
                  {k}
                </span>
              ))}
            </div>
          )}

          {/* Button row */}
          <div className="flex items-center gap-2 pt-0.5">
            {onEdit && (
              <button
                type="button"
                onClick={onEdit}
                disabled={busy}
                className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md border border-border hover:bg-muted disabled:opacity-50"
              >
                <Pencil className="h-3 w-3" /> Edit
              </button>
            )}
            <button
              type="button"
              onClick={onTest}
              disabled={busy}
              className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md border border-border hover:bg-muted disabled:opacity-50"
            >
              {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <PlugZap className="h-3 w-3" />} Test
            </button>
            <button
              type="button"
              onClick={onDiscover}
              disabled={busy}
              className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md border border-border hover:bg-muted disabled:opacity-50"
            >
              <RefreshCw className="h-3 w-3" /> Discover
            </button>
            <button
              type="button"
              onClick={onDelete}
              disabled={busy}
              className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md border border-border text-destructive hover:bg-destructive/10 disabled:opacity-50 ml-auto"
            >
              {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />} Delete
            </button>
          </div>

          {/* Inline test / discover results */}
          {testResult && (
            <div className={`text-xs flex items-center gap-1.5 ${testResult.ok ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"}`}>
              {testResult.ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <AlertTriangle className="h-3.5 w-3.5" />}
              {testResult.ok ? `Connected · ${testResult.latency_ms}ms` : `Test failed: ${testResult.error ?? "unknown"}`}
            </div>
          )}
          {discoverResult && (
            <div className={`text-xs flex items-center gap-1.5 ${discoverResult.ok ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"}`}>
              {discoverResult.ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <AlertTriangle className="h-3.5 w-3.5" />}
              {discoverResult.ok
                ? `Discovered ${discoverResult.count} models — added`
                : `Discover failed: ${discoverResult.error ?? "unknown"}`}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
