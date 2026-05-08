// Mirror of `src/beever_atlas/llm/known_embedding_models.py`.
// Drives:
//   * Auto-fill of the dimensions input when the operator picks a known model.
//   * Cost preview in the migration banner.
//   * Multilingual / Local pill badges.
// Update both files together when adding a new model.

import type { EmbeddingProvider } from "./types";

export interface EmbeddingModelSpec {
  dim: number;
  cost_per_m: number; // USD per million tokens; 0 = free / local
  multilingual: boolean;
  local: boolean;
}

export const SUPPORTED_PROVIDERS: readonly EmbeddingProvider[] = [
  "jina_ai",
  "openai",
  "cohere",
  "voyage",
  "gemini",
  "mistral",
  "ollama",
  "bedrock",
  "vertex_ai",
] as const;

export const PROVIDER_LABELS: Record<EmbeddingProvider, string> = {
  jina_ai: "Jina AI",
  openai: "OpenAI",
  cohere: "Cohere",
  voyage: "Voyage AI",
  gemini: "Google Gemini",
  mistral: "Mistral",
  ollama: "Ollama (local)",
  bedrock: "AWS Bedrock",
  vertex_ai: "Google Vertex AI",
};

export const PROVIDER_KEY_ENV: Record<EmbeddingProvider, string> = {
  jina_ai: "JINA_AI_API_KEY (or legacy JINA_API_KEY)",
  openai: "OPENAI_API_KEY",
  cohere: "COHERE_API_KEY",
  voyage: "VOYAGE_API_KEY",
  gemini: "GEMINI_API_KEY",
  mistral: "MISTRAL_API_KEY",
  ollama: "(none — local)",
  bedrock: "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY",
  vertex_ai: "GOOGLE_APPLICATION_CREDENTIALS",
};

// One-line pitch per provider, surfaced in the tile + help drawer.
export const PROVIDER_DESCRIPTIONS: Record<EmbeddingProvider, string> = {
  jina_ai: "Strong multilingual recall. Default for OSS installs.",
  openai: "Most popular cloud option, multilingual, dimension-resizable.",
  cohere: "Multilingual variant available. Honors `input_type=`.",
  voyage: "Strong multilingual + reranker pairing. Honors `task=`.",
  gemini: "Cheapest cloud multilingual. Reuses your Google API key.",
  mistral: "Hosted by Mistral. Single embedding endpoint.",
  ollama: "Local, free. nomic-embed-text is English-leaning.",
  bedrock: "AWS-hosted. Requires AWS credentials; not in known-models table yet.",
  vertex_ai: "Google Vertex AI. Requires GOOGLE_APPLICATION_CREDENTIALS.",
};

// Default model per provider — the first model in `KNOWN_EMBEDDING_MODELS`
// for that provider. Tile click auto-fills these into the form.
export const PROVIDER_DEFAULT_MODEL: Partial<Record<EmbeddingProvider, string>> = {
  jina_ai: "jina-embeddings-v4",
  openai: "text-embedding-3-large",
  cohere: "embed-multilingual-v3.0",
  voyage: "voyage-3-large",
  gemini: "text-embedding-004",
  mistral: "mistral-embed",
  ollama: "nomic-embed-text",
  // bedrock + vertex_ai have no entry in the known-models table; tile is muted
};

export const KNOWN_EMBEDDING_MODELS: Record<string, EmbeddingModelSpec> = {
  "jina_ai/jina-embeddings-v4": { dim: 2048, cost_per_m: 0.18, multilingual: true, local: false },
  "jina_ai/jina-embeddings-v3": { dim: 1024, cost_per_m: 0.18, multilingual: true, local: false },
  "openai/text-embedding-3-large": { dim: 3072, cost_per_m: 0.13, multilingual: true, local: false },
  "openai/text-embedding-3-small": { dim: 1536, cost_per_m: 0.02, multilingual: true, local: false },
  "voyage/voyage-3-large": { dim: 1024, cost_per_m: 0.18, multilingual: true, local: false },
  "cohere/embed-english-v3.0": { dim: 1024, cost_per_m: 0.10, multilingual: false, local: false },
  "cohere/embed-multilingual-v3.0": { dim: 1024, cost_per_m: 0.10, multilingual: true, local: false },
  "gemini/text-embedding-004": { dim: 768, cost_per_m: 0.025, multilingual: true, local: false },
  "mistral/mistral-embed": { dim: 1024, cost_per_m: 0.10, multilingual: true, local: false },
  "ollama/nomic-embed-text": { dim: 768, cost_per_m: 0.0, multilingual: false, local: true },
  "ollama/mxbai-embed-large": { dim: 1024, cost_per_m: 0.0, multilingual: false, local: true },
};

export function lookupModel(provider: string, model: string): EmbeddingModelSpec | null {
  return KNOWN_EMBEDDING_MODELS[`${provider}/${model}`] ?? null;
}

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
