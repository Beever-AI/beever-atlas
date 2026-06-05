/**
 * Shared types for the ask → render pipeline.
 *
 * Kept in their own module (rather than in index.ts) so pure consumers
 * (sse-client, renderer, tests) can import them without pulling in the
 * server bootstrap side-effects of index.ts.
 */

/**
 * A single supporting source for an answer.
 *
 * `type` is the citation *kind* as emitted by the backend — e.g. `wiki_page`,
 * `channel_message`, `decision_record`, `graph_relationship`, `qa_history`,
 * `media`, `web_result`. The renderer maps it to an icon. The optional fields
 * carry provenance (who said it, where, and a permalink) when the backend
 * supplies them.
 */
export interface Citation {
  type: string;
  text: string;
  author?: string;
  url?: string;
  source?: string;
}

/** A documented tension/contradiction relevant to the answer (proactive nudge). */
export interface Tension {
  title: string;
  detail?: string;
}

/** Assembled result of consuming one `/ask` SSE stream. */
export interface AskResult {
  answer: string;
  citations: Citation[];
  route: string;
  confidence: number;
  costUsd: number;
  /**
   * True when retrieval found no indexed knowledge for the question. Drives the
   * honest empty-state reply instead of surfacing an LLM "I couldn't find…"
   * essay. Derived from a backend `is_empty_retrieval` flag when present, else
   * a conservative client-side heuristic (no citations AND empty-pattern text).
   */
  isEmpty: boolean;
  /** ISO-8601 timestamp of the channel's last sync, when the backend supplies it. */
  lastSyncTs?: string;
  /** Suggested related questions surfaced as chips at the reply tail. */
  followUps?: string[];
  /** Documented tensions relevant to the answer (from the related_context event). */
  tensions?: Tension[];
}
