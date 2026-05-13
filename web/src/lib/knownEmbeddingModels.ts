// Mirror of `src/beever_atlas/llm/known_embedding_models.py`.
// Drives:
//   * Auto-fill of the dimensions input when the operator picks a known model.
//   * Cost preview in the re-embed migration banner.
//   * Multilingual / Local pill badges on the Embedding tab.
// Update both files together when adding a new model.

export interface EmbeddingModelSpec {
  dim: number;
  cost_per_m: number; // USD per million tokens; 0 = free / local
  multilingual: boolean;
  local: boolean;
}

export const KNOWN_EMBEDDING_MODELS: Record<string, EmbeddingModelSpec> = {
  // ── Jina ──────────────────────────────────────────────────────────────
  "jina_ai/jina-embeddings-v4": { dim: 2048, cost_per_m: 0.18, multilingual: true, local: false },
  "jina_ai/jina-embeddings-v3": { dim: 1024, cost_per_m: 0.18, multilingual: true, local: false },
  "jina_ai/jina-embeddings-v2-base-en": { dim: 768, cost_per_m: 0.05, multilingual: false, local: false },
  // ── OpenAI ────────────────────────────────────────────────────────────
  "openai/text-embedding-3-large": { dim: 3072, cost_per_m: 0.13, multilingual: true, local: false },
  "openai/text-embedding-3-small": { dim: 1536, cost_per_m: 0.02, multilingual: true, local: false },
  "openai/text-embedding-ada-002": { dim: 1536, cost_per_m: 0.10, multilingual: true, local: false },
  // ── Voyage ────────────────────────────────────────────────────────────
  "voyage/voyage-3-large": { dim: 1024, cost_per_m: 0.18, multilingual: true, local: false },
  "voyage/voyage-3": { dim: 1024, cost_per_m: 0.06, multilingual: true, local: false },
  "voyage/voyage-3-lite": { dim: 512, cost_per_m: 0.02, multilingual: true, local: false },
  // ── Cohere ────────────────────────────────────────────────────────────
  "cohere/embed-english-v3.0": { dim: 1024, cost_per_m: 0.10, multilingual: false, local: false },
  "cohere/embed-multilingual-v3.0": { dim: 1024, cost_per_m: 0.10, multilingual: true, local: false },
  "cohere/embed-v4.0": { dim: 1536, cost_per_m: 0.12, multilingual: true, local: false },
  // ── Gemini (Google AI) ───────────────────────────────────────────────
  // Per-request embeddings are currently free-tier on the AI Studio key, so
  // we list 0.0 — the honest current value (the table comment notes prices
  // are approximate). gemini-embedding-001's dim is configurable
  // (Matryoshka 128/256/.../3072); 3072 is the natural default.
  "gemini/text-embedding-004": { dim: 768, cost_per_m: 0.0, multilingual: true, local: false },
  "gemini/gemini-embedding-001": { dim: 3072, cost_per_m: 0.0, multilingual: true, local: false },
  // ── Mistral ───────────────────────────────────────────────────────────
  "mistral/mistral-embed": { dim: 1024, cost_per_m: 0.10, multilingual: true, local: false },
  "mistral/codestral-embed": { dim: 1536, cost_per_m: 0.15, multilingual: false, local: false },
  // ── Ollama (local, free) ──────────────────────────────────────────────
  "ollama/nomic-embed-text": { dim: 768, cost_per_m: 0.0, multilingual: false, local: true },
  "ollama/mxbai-embed-large": { dim: 1024, cost_per_m: 0.0, multilingual: false, local: true },
  "ollama/bge-m3": { dim: 1024, cost_per_m: 0.0, multilingual: true, local: true },
  "ollama/snowflake-arctic-embed2": { dim: 1024, cost_per_m: 0.0, multilingual: true, local: true },
  "ollama/all-minilm": { dim: 384, cost_per_m: 0.0, multilingual: false, local: true },
};

export function lookupModel(provider: string, model: string): EmbeddingModelSpec | null {
  return KNOWN_EMBEDDING_MODELS[`${provider}/${model}`] ?? null;
}

/**
 * The known embedding model names for one provider key (e.g. ``"gemini"`` →
 * ``["gemini-embedding-001"]``). Drives the Model ``<select>`` on the
 * Embedding tab — the provider key comes from the chosen endpoint's preset.
 */
export function modelsForProvider(provider: string): string[] {
  const prefix = `${provider}/`;
  return Object.keys(KNOWN_EMBEDDING_MODELS)
    .filter((k) => k.startsWith(prefix))
    .map((k) => k.slice(prefix.length));
}

export function formatCost(spec: EmbeddingModelSpec | null): string {
  if (!spec) return "Cost: unknown";
  if (spec.local || spec.cost_per_m === 0) return "Free (local)";
  return `$${spec.cost_per_m.toFixed(2)} / 1M tokens`;
}

export function estimateMigrationCost(
  factCount: number,
  spec: EmbeddingModelSpec | null,
  // Conservative avg tokens per fact memory_text. Atlas atomic facts
  // empirically run 12-80 tokens; 40 is a midpoint that avoids
  // overstating cost (over-estimate is tolerated; under-estimate erodes trust).
  avgTokensPerFact = 40,
): { dollars: number; tokens: number } {
  if (!spec || spec.local) return { dollars: 0, tokens: 0 };
  const tokens = factCount * avgTokensPerFact;
  const dollars = (tokens / 1_000_000) * spec.cost_per_m;
  return { dollars, tokens };
}

/**
 * Human cost label for a (possibly tiny) dollar amount. A re-embed of a few
 * hundred Atlas facts costs fractions of a cent — rendering "~$0.00" makes the
 * preview look broken, so anything under a cent shows "< $0.01" instead.
 */
export function formatDollars(dollars: number): string {
  if (dollars <= 0) return "$0.00";
  if (dollars < 0.01) return "< $0.01";
  return `~$${dollars.toFixed(2)}`;
}
