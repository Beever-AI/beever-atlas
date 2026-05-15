/**
 * Deduplicate identical sync-error messages and annotate the count.
 *
 * PR-B (extraction-worker spec → "Frontend renders deduped enrichment
 * progress"). When a Gemini 503 storm trips the inline-extraction path,
 * 12+ identical error messages flood ``SyncStatusResponse.errors`` and
 * the UI shows a wall-of-503 banner. Once the dual-read flag is on and
 * the worker takes over, this helper collapses the wall to a single
 * row: ``"AI provider temporarily unavailable (×12 batches)"``.
 *
 * Whitespace-trimmed comparison; preserves the FIRST occurrence of each
 * unique message so error ordering is stable across renders.
 */
export interface DedupedError {
  /** The (trimmed) message text. */
  message: string;
  /** How many raw entries collapsed into this one — always ≥ 1. */
  count: number;
}

export function dedupeErrors(raw: readonly (string | null | undefined)[] | undefined): DedupedError[] {
  if (!raw || raw.length === 0) return [];
  const order: string[] = [];
  const counts = new Map<string, number>();
  for (const entry of raw) {
    if (!entry) continue;
    const msg = entry.trim();
    if (!msg) continue;
    const seen = counts.get(msg);
    if (seen === undefined) {
      order.push(msg);
      counts.set(msg, 1);
    } else {
      counts.set(msg, seen + 1);
    }
  }
  return order.map((message) => ({ message, count: counts.get(message) ?? 1 }));
}

/** Render a deduped list as the legacy ``;``-joined banner string used
 * by callers that expect a single-line summary. Each unique message
 * appears once, with a ``(×N)`` suffix when collapsed. */
export function formatDedupedErrors(deduped: readonly DedupedError[]): string {
  return deduped
    .map(({ message, count }) => (count > 1 ? `${message} (×${count} batches)` : message))
    .join("; ");
}
