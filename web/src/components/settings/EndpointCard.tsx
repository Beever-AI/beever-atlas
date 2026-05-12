import {
  AlertTriangle,
  CheckCircle2,
  CircleDot,
  KeyRound,
  Loader2,
  PlugZap,
  RefreshCw,
  Trash2,
} from "lucide-react";
import type { Endpoint } from "@/lib/aiSetup";

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
  /** True while a Test / Discover / Delete on this endpoint is in flight. */
  busy?: boolean;
  /** Most recent test result (set by the parent after calling onTest). */
  testResult?: EndpointTestResult | null;
  /** Most recent discover result (set by the parent after calling onDiscover). */
  discoverResult?: EndpointDiscoverResult | null;
  onTest: () => void;
  onDiscover: () => void;
  onDelete: () => void;
}

interface StatusPill {
  Icon: typeof CircleDot;
  cls: string;
  label: string;
}

function computeStatus(e: Endpoint, testResult: EndpointTestResult | null | undefined): StatusPill {
  if (e.has_credential === false && e.auth_type !== "none") {
    return { Icon: CircleDot, cls: "text-muted-foreground", label: "no key" };
  }
  if (testResult) {
    return testResult.ok
      ? { Icon: CheckCircle2, cls: "text-green-600 dark:text-green-400", label: `connected · ${testResult.latency_ms}ms` }
      : { Icon: AlertTriangle, cls: "text-destructive", label: `failed: ${testResult.error ?? "unknown"}` };
  }
  if (e.last_test_ok === true) {
    return { Icon: CheckCircle2, cls: "text-green-600 dark:text-green-400", label: "tested ok" };
  }
  if (e.last_test_ok === false) {
    return { Icon: AlertTriangle, cls: "text-destructive", label: e.last_test_error ?? "last test failed" };
  }
  return { Icon: CircleDot, cls: "text-amber-600 dark:text-amber-400", label: "untested" };
}

/**
 * Presentational card for one Endpoint. The parent owns the
 * ``useEndpoints`` hook and the test/discover/delete handlers; this component
 * just renders state. Visual language borrowed from EmbeddingSettings' card
 * chrome — icon box + name + preset + status pill + masked credential, body
 * stats, and a Test / Discover / Delete button row with inline results.
 */
export function EndpointCard({
  endpoint: e,
  usedByCount,
  busy = false,
  testResult,
  discoverResult,
  onTest,
  onDiscover,
  onDelete,
}: EndpointCardProps) {
  const status = computeStatus(e, testResult);
  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden">
      {/* Header strip */}
      <div className="px-4 py-3 border-b border-border flex items-center gap-3">
        <div className="w-9 h-9 rounded-lg bg-muted/60 text-muted-foreground flex items-center justify-center shrink-0">
          <KeyRound className="w-4 h-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-foreground truncate">{e.name}</span>
            <span className="text-xs text-muted-foreground shrink-0">{e.preset}</span>
          </div>
          {e.has_credential ? (
            <div className="text-xs text-muted-foreground font-mono truncate">{e.credential_masked}</div>
          ) : (
            <div className="text-xs text-muted-foreground italic">{e.auth_type === "none" ? "no auth" : "no credential"}</div>
          )}
        </div>
        <span className={`inline-flex items-center gap-1.5 text-xs shrink-0 ${status.cls}`}>
          <status.Icon className="h-3.5 w-3.5" />
          {status.label}
        </span>
      </div>

      {/* Body */}
      <div className="px-4 py-3 space-y-2.5">
        <div className="text-xs text-muted-foreground flex flex-wrap items-center gap-x-1.5">
          <span className="font-mono truncate max-w-full">{e.base_url || "(no base url)"}</span>
          <span>· {e.models.length} models</span>
          <span>· {usedByCount} jobs</span>
          <span>· {e.rpm} RPM</span>
        </div>

        {/* Button row */}
        <div className="flex items-center gap-2">
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
            <Trash2 className="h-3 w-3" /> Delete
          </button>
        </div>

        {/* Inline test / discover results */}
        {testResult && (
          <div className={`text-xs flex items-center gap-1.5 ${testResult.ok ? "text-green-600 dark:text-green-400" : "text-destructive"}`}>
            {testResult.ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <AlertTriangle className="h-3.5 w-3.5" />}
            {testResult.ok ? `Connected · ${testResult.latency_ms}ms` : `Test failed: ${testResult.error ?? "unknown"}`}
          </div>
        )}
        {discoverResult && (
          <div className={`text-xs flex items-center gap-1.5 ${discoverResult.ok ? "text-green-600 dark:text-green-400" : "text-destructive"}`}>
            {discoverResult.ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <AlertTriangle className="h-3.5 w-3.5" />}
            {discoverResult.ok
              ? `Discovered ${discoverResult.count} models — added`
              : `Discover failed: ${discoverResult.error ?? "unknown"}`}
          </div>
        )}
      </div>
    </div>
  );
}
