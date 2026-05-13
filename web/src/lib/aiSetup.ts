/**
 * Type definitions for the Endpoint + Assignment data model (PR-F).
 * Mirrors the Python types in ``llm/endpoints.py``, ``llm/assignments.py``,
 * ``api/endpoints.py``, ``api/assignments.py``.
 *
 * See ``openspec/changes/agent-llm-provider-pluggable/`` for the contract.
 */

export type AuthType =
  | "api_key"
  | "aws_iam"
  | "google_sa"
  | "none"
  | "oauth";

export type PersistedModelKind = "chat" | "embedding";
export type EndpointRole = "chat" | "embedding" | "both" | "auto";

export interface Endpoint {
  id: string;
  name: string;
  preset: string;
  base_url: string;
  auth_type: AuthType;
  has_credential: boolean;
  credential_masked: string;
  models: string[];
  rpm: number;
  headers: Record<string, string>;
  tags: string[];
  last_test_at: string | null;
  last_test_ok: boolean | null;
  last_test_error: string | null;
  created_at: string;
  updated_at: string;
  // PR-α: per-model classification surface. Backend persists these but
  // older documents (and older API versions) may omit them — keep them
  // optional on the TS side so UI code can ?? them.
  model_kinds?: Record<string, PersistedModelKind>;
  advanced_models?: string[];
  manually_kept?: string[];
  role?: EndpointRole;
}

export interface CreateEndpointRequest {
  name: string;
  preset: string;
  base_url?: string;
  auth_type?: AuthType;
  api_key?: string;
  aws_access_key_id?: string;
  aws_secret_access_key?: string;
  aws_region?: string;
  google_sa_json?: string;
  models?: string[];
  rpm?: number;
  headers?: Record<string, string>;
  tags?: string[];
  // PR-β: soft role hint. Omit to let the backend derive a sensible default
  // from the preset (embedding-only presets → "embedding"; rest → "auto").
  role?: EndpointRole;
}

export interface UpdateEndpointRequest {
  name?: string;
  base_url?: string;
  auth_type?: AuthType;
  api_key?: string;
  aws_access_key_id?: string;
  aws_secret_access_key?: string;
  aws_region?: string;
  google_sa_json?: string;
  models?: string[];
  rpm?: number;
  headers?: Record<string, string>;
  tags?: string[];
  // PR-β: omit to leave unchanged. ``manually_kept`` curates the operator-
  // promoted id list — those IDs survive a re-Discover even when the
  // classifier would otherwise drop them into ``advanced_models``.
  role?: EndpointRole;
  manually_kept?: string[];
}

export interface TestConnectionResponse {
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
  // PR-β: the model + kind actually probed (response-only, not persisted).
  probed_model?: string | null;
  probed_kind?: PersistedModelKind | null;
}

export interface DiscoverModelsResponse {
  ok: boolean;
  models: string[];
  error: string | null;
  // PR-α: kept buckets + counts-per-dropped-category. Optional so older
  // backends that don't yet ship these fields still typecheck.
  by_kind?: { chat: string[]; embedding: string[] };
  dropped_breakdown?: Record<string, number>;
}

export type ResponseFormat = "text" | "json";

export interface Assignment {
  consumer: string;
  endpoint_id: string;
  model: string;
  temperature: number | null;
  max_tokens: number | null;
  response_format: ResponseFormat | null;
  extra_headers: Record<string, string>;
  fallback_endpoint_id: string | null;
  dimensions: number | null;
  task: string | null;
  updated_at: string;
}

export interface AssignmentListResponse {
  assignments: Assignment[];
  default_consumers: string[];
  capabilities: Record<string, string[]>; // consumer → required capability tokens
}

export interface UpdateAssignmentRequest {
  endpoint_id: string;
  model: string;
  temperature?: number | null;
  max_tokens?: number | null;
  response_format?: ResponseFormat | null;
  extra_headers?: Record<string, string>;
  fallback_endpoint_id?: string | null;
  dimensions?: number | null;
  task?: string | null;
  force?: boolean;
}

export interface AssignmentSuggestion {
  endpoint_id: string;
  model: string;
}

export interface IncompatibleAssignmentError {
  error: "incompatible_assignment";
  consumer: string;
  model: string;
  missing_capabilities: string[];
  suggested: AssignmentSuggestion[];
}

export interface PresetDiffEntry {
  consumer: string;
  before: Assignment | null;
  after: Assignment;
}

export interface PresetResponse {
  action: "preview" | "applied";
  diff: PresetDiffEntry[];
  preserved: string[];
}

// Capability badge tokens (UI displays one icon per capability).
export const CAPABILITY_ICONS: Record<string, string> = {
  tools: "🔧",
  vision: "👁",
  audio: "🎤",
  "structured-output": "📋",
  streaming: "⚡",
  batch: "📦",
};

// Friendly preset labels for the Quick Start row.
export const PRESET_LABELS: Record<string, string> = {
  "gemini-balanced": "Gemini balanced",
  "openai-quality": "OpenAI quality",
  "claude-quality-gemini-fast": "Claude + Gemini hybrid",
  "fully-local": "Fully local (Ollama)",
  custom: "Custom",
};

// Endpoint preset chips for the Add Endpoint dialog.
export interface EndpointPreset {
  key: string;
  label: string;
  base_url: string;
  auth_type: AuthType;
  default_models: string[];
  embedding_only?: boolean;
  local?: boolean;
  docs_url?: string;
}

export const ENDPOINT_PRESETS: EndpointPreset[] = [
  {
    key: "google_ai",
    label: "Google AI (Gemini)",
    base_url: "https://generativelanguage.googleapis.com/v1beta/openai/",
    auth_type: "api_key",
    default_models: ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"],
    docs_url: "https://aistudio.google.com/apikey",
  },
  {
    key: "openai",
    label: "OpenAI",
    base_url: "https://api.openai.com/v1",
    auth_type: "api_key",
    default_models: ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "o4-mini"],
    docs_url: "https://platform.openai.com/api-keys",
  },
  {
    key: "anthropic",
    label: "Anthropic Claude",
    base_url: "https://api.anthropic.com/v1",
    auth_type: "api_key",
    default_models: ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"],
    docs_url: "https://console.anthropic.com/settings/keys",
  },
  {
    key: "mistral",
    label: "Mistral",
    base_url: "https://api.mistral.ai/v1",
    auth_type: "api_key",
    default_models: ["mistral-small-latest", "mistral-large-latest"],
    docs_url: "https://console.mistral.ai/api-keys",
  },
  {
    key: "deepseek",
    label: "DeepSeek",
    base_url: "https://api.deepseek.com/v1",
    auth_type: "api_key",
    default_models: ["deepseek-chat", "deepseek-reasoner"],
    docs_url: "https://platform.deepseek.com/api_keys",
  },
  {
    key: "groq",
    label: "Groq",
    base_url: "https://api.groq.com/openai/v1",
    auth_type: "api_key",
    default_models: ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
    docs_url: "https://console.groq.com/keys",
  },
  {
    key: "xai",
    label: "xAI Grok",
    base_url: "https://api.x.ai/v1",
    auth_type: "api_key",
    default_models: ["grok-4"],
    docs_url: "https://console.x.ai/team",
  },
  {
    key: "minimax",
    label: "MiniMax",
    base_url: "https://api.minimax.chat/v1",
    auth_type: "api_key",
    default_models: ["abab6.5s-chat"],
    docs_url: "https://platform.minimaxi.com/document/Models",
  },
  {
    key: "voyage",
    label: "Voyage AI",
    base_url: "https://api.voyageai.com/v1",
    auth_type: "api_key",
    default_models: ["voyage-3-large"],
    embedding_only: true,
    docs_url: "https://dash.voyageai.com/",
  },
  {
    key: "jina_ai",
    label: "Jina",
    base_url: "https://api.jina.ai/v1",
    auth_type: "api_key",
    default_models: ["jina-embeddings-v4", "jina-embeddings-v3"],
    embedding_only: true,
    docs_url: "https://jina.ai/api-dashboard/",
  },
  {
    key: "ollama",
    label: "Ollama (local)",
    base_url: "http://localhost:11434/v1",
    auth_type: "none",
    default_models: ["gemma3:e4b", "qwen2.5:14b", "llama3.3", "phi4"],
    local: true,
    docs_url: "https://ollama.com/library",
  },
  {
    key: "openrouter",
    label: "OpenRouter (proxy)",
    base_url: "https://openrouter.ai/api/v1",
    auth_type: "api_key",
    default_models: [],
    docs_url: "https://openrouter.ai/keys",
  },
  {
    key: "custom",
    label: "Custom OpenAI-compatible",
    base_url: "",
    auth_type: "api_key",
    default_models: [],
  },
];

export function getEndpointPreset(key: string): EndpointPreset | undefined {
  return ENDPOINT_PRESETS.find((p) => p.key === key);
}

/**
 * Presets whose providers ship an embeddings endpoint. Used by the Embedding
 * tab to (a) filter which existing endpoints can be picked as the embedding
 * source and (b) restrict the "Add embedding endpoint" preset chips.
 *
 * Heuristic on ``e.preset`` — ``litellm_proxy`` is included because a LiteLLM
 * proxy can front any embedding-capable backend.
 */
export const EMBEDDING_CAPABLE_PRESETS = new Set<string>([
  "jina_ai",
  "voyage",
  "openai",
  "gemini",
  "google_ai",
  "ollama",
  "custom",
  "litellm_proxy",
]);

/** True when an endpoint's preset is one we know can serve embeddings. */
export function endpointSupportsEmbedding(e: Pick<Endpoint, "preset">): boolean {
  return EMBEDDING_CAPABLE_PRESETS.has(e.preset);
}

/** True for ``EndpointPreset``s we offer in the "Add embedding endpoint" picker. */
export function presetSupportsEmbedding(p: EndpointPreset): boolean {
  return EMBEDDING_CAPABLE_PRESETS.has(p.key);
}


/**
 * PR-κ: per-preset link to the provider's model catalog. Surfaces in the
 * endpoint card + Add Endpoint form so operators can see what models
 * their account has access to before typing a model id by hand. Each
 * URL points at the provider's official model list; ``custom`` and
 * unknown presets fall back to LiteLLM's provider-agnostic docs.
 *
 * Separate from ``EndpointPreset.docs_url`` (which goes to the API-key
 * settings page, not the model catalog).
 */
const PRESET_MODELS_URL: Record<string, string> = {
  openai: "https://platform.openai.com/docs/models",
  google_ai: "https://ai.google.dev/gemini-api/docs/models",
  anthropic: "https://docs.anthropic.com/en/docs/about-claude/models/overview",
  mistral: "https://docs.mistral.ai/getting-started/models/models_overview/",
  deepseek: "https://api-docs.deepseek.com/quick_start/pricing",
  groq: "https://console.groq.com/docs/models",
  xai: "https://docs.x.ai/docs/models",
  minimax: "https://platform.minimaxi.com/document/Models",
  cohere: "https://docs.cohere.com/docs/models",
  voyage: "https://docs.voyageai.com/docs/embeddings",
  jina_ai: "https://jina.ai/embeddings/",
  together_ai: "https://docs.together.ai/docs/serverless-models",
  ollama: "https://ollama.com/library",
  ollama_chat: "https://ollama.com/library",
  vllm: "https://docs.vllm.ai/en/latest/models/supported_models.html",
  lmstudio: "https://lmstudio.ai/models",
  openrouter: "https://openrouter.ai/models",
  litellm_proxy: "https://docs.litellm.ai/docs/providers",
  bedrock: "https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html",
  vertex_ai: "https://cloud.google.com/vertex-ai/generative-ai/docs/learn/models",
};

/**
 * Return the URL for the provider's official model catalog.
 *
 * Falls back to LiteLLM's provider index for ``custom`` / unknown presets —
 * the operator chose a custom URL, so the most useful generic doc is
 * LiteLLM's "which providers are supported" page.
 */
export function modelsDocsUrl(preset: string): string {
  return PRESET_MODELS_URL[preset] ?? "https://docs.litellm.ai/docs/providers";
}

export function formatCost(spec: { input_cost_per_m?: number; output_cost_per_m?: number; cost_per_m?: number }): string {
  if (typeof spec.cost_per_m === "number") {
    return spec.cost_per_m === 0 ? "free" : `~$${spec.cost_per_m.toFixed(2)}/M`;
  }
  if (spec.input_cost_per_m != null && spec.output_cost_per_m != null) {
    if (spec.input_cost_per_m === 0 && spec.output_cost_per_m === 0) return "free";
    return `~$${spec.input_cost_per_m.toFixed(2)}/M in · $${spec.output_cost_per_m.toFixed(2)}/M out`;
  }
  return "—";
}

export function maskCredential(plaintext: string): string {
  if (!plaintext) return "";
  if (plaintext.length < 8) return "***";
  return `${plaintext.slice(0, 4)}...${plaintext.slice(-4)}`;
}
