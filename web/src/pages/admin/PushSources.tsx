import { useCallback, useEffect, useState } from "react";
import { Plus, RotateCw, Trash2, Copy, Check, X, ShieldAlert } from "lucide-react";
import { api, adminHeaders } from "@/lib/api";

// ---------------------------------------------------------------------------
// Modal a11y helper
// ---------------------------------------------------------------------------

/** Close the modal on Escape so keyboard users have a non-mouse exit. */
function useEscapeToClose(active: boolean, onClose: () => void) {
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, onClose]);
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface PushSource {
  source_id: string;
  allowed_channels_pattern: string;
  description?: string;
  last_event_ts: string | null;
  idempotency_replay_count_24h: number;
  breaker_state: string;
  created_at: string;
  rotated_at: string | null;
  secret_fingerprint?: string;
}

interface RegisterPayload {
  source_id: string;
  allowed_channels_pattern: string;
  description?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtTs(ts: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return ts;
  }
}

// ---------------------------------------------------------------------------
// SecretRevealModal
// ---------------------------------------------------------------------------

interface SecretRevealModalProps {
  secret: string;
  onClose: () => void;
}

function SecretRevealModal({ secret, onClose }: SecretRevealModalProps) {
  const [copied, setCopied] = useState(false);
  useEscapeToClose(true, onClose);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard unavailable — user can select manually
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Source secret"
    >
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />
      <div className="relative z-10 w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
        <div className="px-6 py-4 border-b border-border flex items-center justify-between">
          <h2 className="text-base font-semibold text-foreground">Source secret</h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded hover:bg-muted transition-colors"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>
        <div className="p-6 space-y-4">
          <div className="rounded-lg border border-amber-200 dark:border-amber-900/50 bg-amber-50 dark:bg-amber-950/30 px-4 py-3 text-sm text-amber-700 dark:text-amber-300 flex items-start gap-2">
            <ShieldAlert size={16} className="shrink-0 mt-0.5" />
            <span>This secret will not be shown again. Save it now.</span>
          </div>
          <div>
            <label className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              HMAC secret
            </label>
            <div className="mt-1.5 flex items-center gap-2">
              <code className="flex-1 block rounded-md bg-muted px-3 py-2 text-sm font-mono break-all text-foreground select-all">
                {secret}
              </code>
              <button
                type="button"
                onClick={handleCopy}
                className="shrink-0 inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-border text-sm hover:bg-muted transition-colors"
                aria-label="Copy secret to clipboard"
              >
                {copied ? <Check size={14} className="text-emerald-500" /> : <Copy size={14} />}
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
          </div>
        </div>
        <div className="px-6 pb-5">
          <button
            type="button"
            onClick={onClose}
            className="w-full inline-flex items-center justify-center px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
          >
            I have saved the secret
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// RegisterSourceModal
// ---------------------------------------------------------------------------

interface RegisterSourceModalProps {
  onClose: () => void;
  onCreated: (secret: string, source: PushSource) => void;
}

function RegisterSourceModal({ onClose, onCreated }: RegisterSourceModalProps) {
  const [sourceId, setSourceId] = useState("");
  const [pattern, setPattern] = useState("*");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEscapeToClose(true, onClose);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!sourceId.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const body: RegisterPayload = {
        source_id: sourceId.trim(),
        allowed_channels_pattern: pattern.trim() || "*",
        description: description.trim() || undefined,
      };
      const resp = await api.post<{ source: PushSource; secret: string }>(
        "/api/admin/sources",
        body,
        { headers: adminHeaders() },
      );
      onCreated(resp.secret, resp.source);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to register source");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Register source"
    >
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />
      <div className="relative z-10 w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
        <div className="px-6 py-4 border-b border-border flex items-center justify-between">
          <h2 className="text-base font-semibold text-foreground">Register source</h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded hover:bg-muted transition-colors"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div>
            <label className="block text-sm font-medium text-foreground mb-1" htmlFor="reg-source-id">
              Source ID
            </label>
            <input
              id="reg-source-id"
              type="text"
              value={sourceId}
              onChange={(e) => setSourceId(e.target.value)}
              placeholder="openclaw-prod"
              required
              className="w-full h-9 rounded-lg border border-border bg-card px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-foreground mb-1" htmlFor="reg-pattern">
              Allowed channels pattern
            </label>
            <input
              id="reg-pattern"
              type="text"
              value={pattern}
              onChange={(e) => setPattern(e.target.value)}
              placeholder="*"
              className="w-full h-9 rounded-lg border border-border bg-card px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <p className="mt-1 text-[11px] text-muted-foreground">
              Glob pattern. Use <code>*</code> to allow all channels.
            </p>
          </div>
          <div>
            <label className="block text-sm font-medium text-foreground mb-1" htmlFor="reg-desc">
              Description (optional)
            </label>
            <input
              id="reg-desc"
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Production webhook sender"
              className="w-full h-9 rounded-lg border border-border bg-card px-3 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          {error && (
            <p className="text-sm text-rose-600 dark:text-rose-400">{error}</p>
          )}
          <div className="flex items-center gap-2 pt-1">
            <button
              type="submit"
              disabled={submitting || !sourceId.trim()}
              className="flex-1 inline-flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? "Registering..." : "Register"}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 rounded-lg border border-border text-sm text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ConfirmModal
// ---------------------------------------------------------------------------

interface ConfirmModalProps {
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onClose: () => void;
  danger?: boolean;
}

function ConfirmModal({ title, message, confirmLabel = "Confirm", onConfirm, onClose, danger = false }: ConfirmModalProps) {
  // Disable the confirm button while ``onConfirm`` is in flight so a
  // double-click cannot fire two rotate / delete requests. The parent
  // closes the modal after the promise resolves; if it doesn't resolve
  // we leave the button disabled so the user knows something is happening.
  const [confirming, setConfirming] = useState(false);
  useEscapeToClose(true, onClose);

  async function handleConfirm() {
    if (confirming) return;
    setConfirming(true);
    try {
      await Promise.resolve(onConfirm());
    } finally {
      setConfirming(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />
      <div className="relative z-10 w-full max-w-sm bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
        <div className="px-6 py-4 border-b border-border">
          <h2 className="text-base font-semibold text-foreground">{title}</h2>
        </div>
        <div className="p-6">
          <p className="text-sm text-muted-foreground">{message}</p>
        </div>
        <div className="px-6 pb-5 flex items-center gap-2">
          <button
            type="button"
            onClick={handleConfirm}
            disabled={confirming}
            className={`flex-1 inline-flex items-center justify-center px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
              danger
                ? "bg-rose-600 text-white hover:bg-rose-700"
                : "bg-primary text-primary-foreground hover:bg-primary/90"
            }`}
          >
            {confirming ? "..." : confirmLabel}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 rounded-lg border border-border text-sm text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function PushSources() {
  const adminToken = (import.meta.env.VITE_BEEVER_ADMIN_TOKEN as string | undefined) ?? "";
  const hasAdminToken = adminToken.length > 0;

  const [sources, setSources] = useState<PushSource[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Modal state
  const [showRegister, setShowRegister] = useState(false);
  const [revealSecret, setRevealSecret] = useState<string | null>(null);
  const [rotatingId, setRotatingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const fetchSources = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<PushSource[]>("/api/admin/sources", {
        headers: adminHeaders(),
      });
      setSources(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load sources");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!hasAdminToken) return;
    void fetchSources();
  }, [hasAdminToken, fetchSources]);

  async function handleRotateConfirm() {
    if (!rotatingId) return;
    try {
      const resp = await api.patch<{ secret: string; source: PushSource }>(
        `/api/admin/sources/${encodeURIComponent(rotatingId)}/rotate`,
        {},
        { headers: adminHeaders() },
      );
      setRevealSecret(resp.secret);
      setSources((prev) =>
        prev.map((s) => (s.source_id === rotatingId ? resp.source : s)),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rotate failed");
    } finally {
      setRotatingId(null);
    }
  }

  async function handleDeleteConfirm() {
    if (!deletingId) return;
    const id = deletingId;
    setDeletingId(null);
    // Optimistic update
    setSources((prev) => prev.filter((s) => s.source_id !== id));
    try {
      await api.delete(`/api/admin/sources/${encodeURIComponent(id)}`, {
        headers: adminHeaders(),
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
      // Restore on failure
      void fetchSources();
    }
  }

  function handleCreated(secret: string, source: PushSource) {
    setShowRegister(false);
    setSources((prev) => [...prev, source]);
    setRevealSecret(secret);
  }

  if (!hasAdminToken) {
    return (
      <div className="h-full overflow-auto">
        <div className="p-6 max-w-6xl mx-auto">
          <div className="flex flex-col items-center justify-center py-20 text-center">
            <ShieldAlert className="w-10 h-10 text-muted-foreground mb-3" />
            <h2 className="text-lg font-semibold text-foreground">Access denied</h2>
            <p className="mt-1 text-sm text-muted-foreground max-w-sm">
              This page requires an admin token. Set{" "}
              <code className="text-xs font-mono">VITE_BEEVER_ADMIN_TOKEN</code> in{" "}
              <code className="text-xs font-mono">web/.env.local</code>.
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto">
      <div className="p-6 max-w-6xl mx-auto">
        {/* Header */}
        <div className="mb-6 flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-foreground tracking-tight">Push Sources</h1>
            <p className="text-sm text-muted-foreground mt-1">
              Registered external sources that can push events to the Beever ingestion pipeline.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setShowRegister(true)}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors shrink-0"
          >
            <Plus className="w-4 h-4" />
            Register source
          </button>
        </div>

        {/* Error */}
        {error && (
          <div className="mb-4 rounded-lg border border-rose-200 dark:border-rose-900 bg-rose-50 dark:bg-rose-950/30 px-4 py-3 text-sm text-rose-700 dark:text-rose-300">
            {error}
          </div>
        )}

        {/* Table */}
        {loading ? (
          <div className="space-y-2">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-14 rounded-xl bg-muted/40 animate-pulse" />
            ))}
          </div>
        ) : sources.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 px-6 rounded-2xl border-2 border-dashed border-border">
            <h2 className="text-base font-semibold text-foreground mb-1">No sources registered</h2>
            <p className="text-sm text-muted-foreground text-center max-w-md mb-5">
              Register your first push source to allow external services to send events to Beever.
            </p>
            <button
              type="button"
              onClick={() => setShowRegister(true)}
              className="inline-flex items-center gap-1.5 px-5 py-2.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
            >
              <Plus className="w-4 h-4" />
              Register source
            </button>
          </div>
        ) : (
          <div className="rounded-xl border border-border overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border bg-muted/30">
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                      Source ID
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                      Allowed pattern
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                      Secret fingerprint
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                      Last event
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                      Replays (24h)
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                      Breaker
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                      Created
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wide">
                      Rotated
                    </th>
                    <th className="px-4 py-3" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {sources.map((source) => (
                    <tr key={source.source_id} className="bg-card hover:bg-muted/20 transition-colors">
                      <td className="px-4 py-3 font-mono text-xs text-foreground">
                        {source.source_id}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                        {source.allowed_channels_pattern}
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                        {source.secret_fingerprint ?? "—"}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">
                        {fmtTs(source.last_event_ts)}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground text-center">
                        {source.idempotency_replay_count_24h}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-block rounded-full px-2 py-0.5 text-[10px] font-medium ${
                            source.breaker_state === "open"
                              ? "bg-rose-100 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300"
                              : source.breaker_state === "half-open"
                              ? "bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300"
                              : "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300"
                          }`}
                        >
                          {source.breaker_state ?? "closed"}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">
                        {fmtTs(source.created_at)}
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">
                        {fmtTs(source.rotated_at)}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-1.5 justify-end">
                          <button
                            type="button"
                            onClick={() => setRotatingId(source.source_id)}
                            className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-border hover:bg-muted transition-colors"
                            title="Rotate secret"
                          >
                            <RotateCw size={11} />
                            Rotate
                          </button>
                          <button
                            type="button"
                            onClick={() => setDeletingId(source.source_id)}
                            className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-md border border-rose-200 dark:border-rose-900 text-rose-600 dark:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-950/30 transition-colors"
                            title="Delete source"
                          >
                            <Trash2 size={11} />
                            Delete
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {/* Modals */}
      {showRegister && (
        <RegisterSourceModal
          onClose={() => setShowRegister(false)}
          onCreated={handleCreated}
        />
      )}

      {revealSecret && (
        <SecretRevealModal
          secret={revealSecret}
          onClose={() => setRevealSecret(null)}
        />
      )}

      {rotatingId && (
        <ConfirmModal
          title="Rotate secret"
          message={`Rotating the secret for "${rotatingId}" will immediately invalidate all existing signatures. Any senders using the old secret will start receiving 401 errors until they update their key.`}
          confirmLabel="Rotate secret"
          onConfirm={handleRotateConfirm}
          onClose={() => setRotatingId(null)}
        />
      )}

      {deletingId && (
        <ConfirmModal
          title="Delete source"
          message={`Delete "${deletingId}"? All subsequent push events for this source ID will return 404. This cannot be undone.`}
          confirmLabel="Delete"
          danger
          onConfirm={handleDeleteConfirm}
          onClose={() => setDeletingId(null)}
        />
      )}
    </div>
  );
}
