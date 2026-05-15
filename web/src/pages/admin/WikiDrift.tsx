import { useCallback, useEffect, useRef, useState } from "react";
import { ShieldAlert, AlertTriangle, CheckCircle2, XCircle } from "lucide-react";
import { api, adminHeaders } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface DriftChannel {
  channel_id: string;
  page_count: number;
  levenshtein_section_p50_median: number;
  levenshtein_section_p95_median: number;
  last_run_ts: string | null;
  pass_criterion_met: boolean;
}

interface DriftSummary {
  channels: DriftChannel[];
  pass: boolean;
  data_fresh: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

function fmtNumber(n: number, digits = 3): string {
  if (typeof n !== "number" || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function fmtRelativeTime(ts: string | null): string {
  if (!ts) return "—";
  try {
    const t = new Date(ts);
    const diffMs = Date.now() - t.getTime();
    const diffMin = Math.round(diffMs / 60_000);
    if (diffMin < 1) return "just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.round(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.round(diffHr / 24);
    return `${diffDay}d ago`;
  } catch {
    return ts;
  }
}

function worstP50(channels: DriftChannel[]): number {
  let worst = 0;
  for (const c of channels) {
    if (c.levenshtein_section_p50_median > worst) {
      worst = c.levenshtein_section_p50_median;
    }
  }
  return worst;
}

function failingChannelCount(channels: DriftChannel[]): number {
  return channels.filter((c) => !c.pass_criterion_met).length;
}

// ---------------------------------------------------------------------------
// Banner
// ---------------------------------------------------------------------------

interface BannerProps {
  summary: DriftSummary;
}

function StatusBanner({ summary }: BannerProps) {
  if (summary.pass) {
    return (
      <div
        role="status"
        className="rounded-lg border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/30 px-4 py-3 text-sm text-emerald-800 dark:text-emerald-200 flex items-start gap-2"
        data-testid="drift-banner-pass"
      >
        <CheckCircle2 size={18} className="shrink-0 mt-0.5" />
        <span className="font-medium">
          PASSING — soak threshold met across {summary.channels.length} channel
          {summary.channels.length === 1 ? "" : "s"}
        </span>
      </div>
    );
  }
  const worst = worstP50(summary.channels);
  const failing = failingChannelCount(summary.channels);
  return (
    <div
      role="alert"
      className="rounded-lg border border-rose-200 dark:border-rose-900 bg-rose-50 dark:bg-rose-950/30 px-4 py-3 text-sm text-rose-800 dark:text-rose-200 flex items-start gap-2"
      data-testid="drift-banner-fail"
    >
      <XCircle size={18} className="shrink-0 mt-0.5" />
      <span className="font-medium">
        FAILING — drift {fmtNumber(worst, 2)} exceeds threshold on {failing}{" "}
        channel{failing === 1 ? "" : "s"}
      </span>
    </div>
  );
}

function DataFreshnessWarning() {
  return (
    <div
      role="status"
      className="rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30 px-4 py-3 text-sm text-amber-800 dark:text-amber-200 flex items-start gap-2"
      data-testid="drift-banner-stale"
    >
      <AlertTriangle size={18} className="shrink-0 mt-0.5" />
      <span>
        WARNING — last drift report is more than 1 hour old. The comparator may
        have stalled or <code className="text-xs font-mono">WIKI_DRIFT_AB</code>{" "}
        may have been turned off mid-soak.
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Channel table
// ---------------------------------------------------------------------------

interface TableProps {
  channels: DriftChannel[];
}

function ChannelTable({ channels }: TableProps) {
  if (channels.length === 0) {
    return (
      <div
        className="rounded-2xl border-2 border-dashed border-border px-6 py-12 text-center text-sm text-muted-foreground"
        data-testid="drift-empty"
      >
        No drift reports in the selected window. Confirm{" "}
        <code className="text-xs font-mono">WIKI_DRIFT_AB=true</code> is set on
        the staging environment for at least one channel.
      </div>
    );
  }
  return (
    <div className="rounded-xl border border-border overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/30">
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                Channel
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                Reports
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                Median p50
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                Median p95
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                Last run
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                Threshold
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {channels.map((c) => (
              <tr
                key={c.channel_id}
                className={
                  c.pass_criterion_met
                    ? "bg-card hover:bg-muted/20 transition-colors"
                    : "bg-rose-50/50 dark:bg-rose-950/10 hover:bg-rose-100/40 dark:hover:bg-rose-950/30 transition-colors"
                }
                data-testid={`drift-row-${c.channel_id}`}
              >
                <td className="px-4 py-3 font-mono text-xs text-foreground">
                  {c.channel_id}
                </td>
                <td className="px-4 py-3 text-xs text-muted-foreground">
                  {c.page_count}
                </td>
                <td className="px-4 py-3 text-xs text-foreground">
                  {fmtNumber(c.levenshtein_section_p50_median, 3)}
                </td>
                <td className="px-4 py-3 text-xs text-foreground">
                  {fmtNumber(c.levenshtein_section_p95_median, 3)}
                </td>
                <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">
                  {fmtRelativeTime(c.last_run_ts)}
                </td>
                <td className="px-4 py-3">
                  {c.pass_criterion_met ? (
                    <span className="text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1">
                      <CheckCircle2 size={14} aria-hidden="true" />
                      <span className="sr-only">passing</span>✓
                    </span>
                  ) : (
                    <span className="text-rose-600 dark:text-rose-400 inline-flex items-center gap-1">
                      <XCircle size={14} aria-hidden="true" />
                      <span className="sr-only">failing</span>✗
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function WikiDrift() {
  const adminToken =
    (import.meta.env.VITE_BEEVER_ADMIN_TOKEN as string | undefined) ?? "";
  const hasAdminToken = adminToken.length > 0;

  const [summary, setSummary] = useState<DriftSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Don't blow away an existing summary while a refresh is mid-flight —
  // the dashboard should never flicker to a loading state during the
  // 5-min auto-refresh.
  const isInitialFetch = useRef(true);

  const fetchSummary = useCallback(async () => {
    if (isInitialFetch.current) setLoading(true);
    setError(null);
    try {
      const data = await api.get<DriftSummary>(
        "/api/admin/wiki-drift/summary?days=14",
        { headers: adminHeaders() },
      );
      setSummary(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load drift summary");
    } finally {
      setLoading(false);
      isInitialFetch.current = false;
    }
  }, []);

  useEffect(() => {
    if (!hasAdminToken) return;
    void fetchSummary();
    const id = setInterval(() => {
      void fetchSummary();
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [hasAdminToken, fetchSummary]);

  if (!hasAdminToken) {
    return (
      <div className="h-full overflow-auto">
        <div className="p-6 max-w-6xl mx-auto">
          <div className="flex flex-col items-center justify-center py-20 text-center">
            <ShieldAlert className="w-10 h-10 text-muted-foreground mb-3" />
            <h2 className="text-lg font-semibold text-foreground">
              Access denied
            </h2>
            <p className="mt-1 text-sm text-muted-foreground max-w-sm">
              This page requires an admin token. Set{" "}
              <code className="text-xs font-mono">VITE_BEEVER_ADMIN_TOKEN</code>{" "}
              in <code className="text-xs font-mono">web/.env.local</code>.
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto">
      <div className="p-6 max-w-6xl mx-auto space-y-4">
        <header>
          <h1 className="text-2xl font-semibold text-foreground tracking-tight">
            Wiki drift soak
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            Per-channel drift between incremental and from-scratch wiki
            generation over the last 14 days. The soak passes when every
            channel's median Levenshtein stays under 0.15 and p95 under 0.30.
          </p>
        </header>

        {error && (
          <div className="rounded-lg border border-rose-200 dark:border-rose-900 bg-rose-50 dark:bg-rose-950/30 px-4 py-3 text-sm text-rose-700 dark:text-rose-300">
            {error}
          </div>
        )}

        {loading && !summary ? (
          <div className="space-y-2">
            <div className="h-12 rounded-xl bg-muted/40 animate-pulse" />
            <div className="h-12 rounded-xl bg-muted/40 animate-pulse" />
            <div className="h-32 rounded-xl bg-muted/40 animate-pulse" />
          </div>
        ) : summary ? (
          <>
            {/* Render the pass/fail banner ONLY when we have data to score
             * against. With an empty collection the API returns
             * ``{channels: [], pass: false, ...}`` which the banner
             * would otherwise mis-render as a red "FAILING — drift 0.00
             * exceeds threshold on 0 channels" — caught during fresh
             * dogfood (Alan, 2026-05-02). The ChannelTable's own
             * empty-state copy guides operators to flip
             * ``WIKI_DRIFT_AB=true`` so the dashboard cleanly says
             * "awaiting data" instead of "failing" before any data
             * exists. */}
            {summary.channels.length > 0 && <StatusBanner summary={summary} />}
            {!summary.data_fresh && summary.channels.length > 0 && (
              <DataFreshnessWarning />
            )}
            <ChannelTable channels={summary.channels} />
          </>
        ) : null}
      </div>
    </div>
  );
}
