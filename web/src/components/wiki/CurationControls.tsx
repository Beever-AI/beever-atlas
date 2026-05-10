/**
 * Per-page curation dropdown + "Apply Pending Updates" button.
 *
 * wiki-redesign-gap-fill / Group 4 — operator-visible control over a page's
 * rewrite cadence. Three modes:
 *
 *  - **auto** (default) — maintainer marks dirty AND applies LLM patches.
 *  - **manual** — maintainer marks dirty but does NOT auto-apply.
 *  - **frozen** — maintainer skips entirely; Builder also skips.
 *
 * Persists via `PATCH /api/channels/{id}/wiki/pages/{slug}/curation`. The
 * "Apply Pending Updates" button appears in the WikiTab header when at least
 * one manual-mode page has pending dirty facts.
 */

import { useState } from "react";
import { Lock, Clock } from "lucide-react";
import { api } from "@/lib/api";

export type CurationMode = "auto" | "manual" | "frozen";

interface CurationDropdownProps {
  channelId: string;
  slug: string;
  curationMode: CurationMode;
  targetLang?: string;
  onChange?: (mode: CurationMode) => void;
}

/** Per-page dropdown — renders next to the page title. */
export function CurationDropdown({
  channelId,
  slug,
  curationMode,
  targetLang,
  onChange,
}: CurationDropdownProps) {
  const [mode, setMode] = useState<CurationMode>(curationMode);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleChange = async (next: CurationMode) => {
    setBusy(true);
    setError(null);
    const prior = mode;
    setMode(next); // optimistic
    try {
      const langParam = targetLang
        ? `?target_lang=${encodeURIComponent(targetLang)}`
        : "";
      await api.patch(
        `/api/channels/${channelId}/wiki/pages/${slug}/curation${langParam}`,
        { curation_mode: next },
      );
      onChange?.(next);
    } catch (err) {
      setMode(prior);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      <CurationBadge mode={mode} />
      <select
        className="rounded border bg-background px-2 py-1 text-xs"
        value={mode}
        disabled={busy}
        onChange={(e) => handleChange(e.target.value as CurationMode)}
        aria-label="Curation mode"
      >
        <option value="auto">Auto</option>
        <option value="manual">Manual</option>
        <option value="frozen">Frozen</option>
      </select>
      {error && (
        <span className="text-xs text-red-500" title={error}>
          (failed)
        </span>
      )}
    </div>
  );
}

/** Badge — lock for frozen, clock for manual, nothing for auto. */
export function CurationBadge({ mode }: { mode: CurationMode }) {
  if (mode === "frozen") {
    return (
      <span
        className="inline-flex items-center gap-1 text-xs text-muted-foreground"
        title="Frozen — maintainer and Builder skip this page."
      >
        <Lock size={12} aria-hidden /> frozen
      </span>
    );
  }
  if (mode === "manual") {
    return (
      <span
        className="inline-flex items-center gap-1 text-xs text-muted-foreground"
        title="Manual — pending updates wait for operator approval."
      >
        <Clock size={12} aria-hidden /> manual
      </span>
    );
  }
  return null;
}

interface ApplyPendingUpdatesButtonProps {
  channelId: string;
  manualDirtyCount: number;
  targetLang?: string;
  onApplied?: () => void;
}

/**
 * Header button — only renders when ≥1 manual-mode page has dirty facts.
 * Triggers `POST /api/channels/{id}/wiki/apply-pending-updates`.
 */
export function ApplyPendingUpdatesButton({
  channelId,
  manualDirtyCount,
  targetLang,
  onApplied,
}: ApplyPendingUpdatesButtonProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (manualDirtyCount <= 0) return null;

  const handleClick = async () => {
    setBusy(true);
    setError(null);
    try {
      const langParam = targetLang
        ? `?target_lang=${encodeURIComponent(targetLang)}`
        : "";
      await api.post(
        `/api/channels/${channelId}/wiki/apply-pending-updates${langParam}`,
      );
      onApplied?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      onClick={handleClick}
      disabled={busy}
      className="rounded border bg-background px-3 py-1 text-xs hover:bg-muted disabled:opacity-50"
      title={
        error
          ? `Apply failed: ${error}`
          : `Flush ${manualDirtyCount} manual-mode page(s) with pending updates`
      }
    >
      {busy
        ? "Applying…"
        : `Apply Pending Updates (${manualDirtyCount} page${manualDirtyCount === 1 ? "" : "s"})`}
    </button>
  );
}
