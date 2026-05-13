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
  "jina_ai/jina-embeddings-v4": { dim: 2048, cost_per_m: 0.18, multilingual: true, local: false },
  "jina_ai/jina-embeddings-v3": { dim: 1024, cost_per_m: 0.18, multilingual: true, local: false },
  "openai/text-embedding-3-large": { dim: 3072, cost_per_m: 0.13, multilingual: true, local: false },
  "openai/text-embedding-3-small": { dim: 1536, cost_per_m: 0.02, multilingual: true, local: false },
  "voyage/voyage-3-large": { dim: 1024, cost_per_m: 0.18, multilingual: true, local: false },
  "cohere/embed-english-v3.0": { dim: 1024, cost_per_m: 0.10, multilingual: false, local: false },
  "cohere/embed-multilingual-v3.0": { dim: 1024, cost_per_m: 0.10, multilingual: true, local: false },
  "gemini/gemini-embedding-001": { dim: 3072, cost_per_m: 0.025, multilingual: true, local: false },
  "mistral/mistral-embed": { dim: 1024, cost_per_m: 0.10, multilingual: true, local: false },
  "ollama/nomic-embed-text": { dim: 768, cost_per_m: 0.0, multilingual: false, local: true },
  "ollama/mxbai-embed-large": { dim: 1024, cost_per_m: 0.0, multilingual: false, local: true },
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
