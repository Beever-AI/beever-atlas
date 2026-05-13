/**
 * Lightweight TS mirror of the Python ``KNOWN_MODELS`` catalog
 * (``src/beever_atlas/llm/known_models.py``) — only the fields the AI Setup
 * UI needs: capability flags + per-million-token cost. Drives capability
 * badges, the cost-per-row hint, and the cost rollup.
 *
 * Not exhaustive — models absent here fall back to the heuristic in
 * ``inferCapabilities`` below (a port of ``llm/capability_infer.py``).
 *
 * NOTE: this is duplicated from the Python catalog (see review MEDIUM-5).
 * A follow-up will serve the catalog from ``GET /api/settings/models`` so
 * the two can't drift; for now keep the high-traffic entries in sync.
 */

export interface ModelSpec {
  kind: "chat" | "embedding" | "both";
  context_window?: number;
  input_cost_per_m?: number;
  output_cost_per_m?: number;
  cost_per_m?: number;
  supports_tools?: boolean;
  supports_vision?: boolean;
  supports_audio?: boolean;
  supports_streaming?: boolean;
  local?: boolean;
}

export const KNOWN_MODELS: Record<string, ModelSpec> = {
  // Chat — Gemini
  "gemini/gemini-2.5-flash": { kind: "chat", context_window: 1_000_000, input_cost_per_m: 0.3, output_cost_per_m: 2.5, supports_tools: true, supports_vision: true, supports_audio: true, supports_streaming: true },
  "gemini/gemini-2.5-flash-lite": { kind: "chat", context_window: 1_000_000, input_cost_per_m: 0.1, output_cost_per_m: 0.4, supports_tools: true, supports_vision: true, supports_audio: true, supports_streaming: true },
  "gemini/gemini-2.5-pro": { kind: "chat", context_window: 2_000_000, input_cost_per_m: 1.25, output_cost_per_m: 10, supports_tools: true, supports_vision: true, supports_audio: true, supports_streaming: true },
  // Chat — OpenAI
  "openai/gpt-4o-mini": { kind: "chat", context_window: 128_000, input_cost_per_m: 0.15, output_cost_per_m: 0.6, supports_tools: true, supports_vision: true, supports_streaming: true },
  "openai/gpt-4o": { kind: "chat", context_window: 128_000, input_cost_per_m: 2.5, output_cost_per_m: 10, supports_tools: true, supports_vision: true, supports_streaming: true },
  "openai/gpt-4.1": { kind: "chat", context_window: 1_000_000, input_cost_per_m: 3, output_cost_per_m: 12, supports_tools: true, supports_vision: true, supports_streaming: true },
  "openai/o4-mini": { kind: "chat", context_window: 200_000, input_cost_per_m: 1.1, output_cost_per_m: 4.4, supports_tools: true, supports_vision: false, supports_streaming: true },
  // Chat — Anthropic
  "anthropic/claude-haiku-4-5": { kind: "chat", context_window: 200_000, input_cost_per_m: 1, output_cost_per_m: 5, supports_tools: true, supports_vision: true, supports_streaming: true },
  "anthropic/claude-sonnet-4-6": { kind: "chat", context_window: 200_000, input_cost_per_m: 3, output_cost_per_m: 15, supports_tools: true, supports_vision: true, supports_streaming: true },
  "anthropic/claude-opus-4-7": { kind: "chat", context_window: 200_000, input_cost_per_m: 15, output_cost_per_m: 75, supports_tools: true, supports_vision: true, supports_streaming: true },
  // Chat — others
  "mistral/mistral-small-latest": { kind: "chat", context_window: 128_000, input_cost_per_m: 0.2, output_cost_per_m: 0.6, supports_tools: true, supports_streaming: true },
  "mistral/mistral-large-latest": { kind: "chat", context_window: 128_000, input_cost_per_m: 2, output_cost_per_m: 6, supports_tools: true, supports_streaming: true },
  "deepseek/deepseek-chat": { kind: "chat", context_window: 64_000, input_cost_per_m: 0.27, output_cost_per_m: 1.1, supports_tools: true, supports_streaming: true },
  "deepseek/deepseek-reasoner": { kind: "chat", context_window: 64_000, input_cost_per_m: 0.55, output_cost_per_m: 2.19, supports_tools: false, supports_streaming: true },
  "groq/llama-3.3-70b-versatile": { kind: "chat", context_window: 128_000, input_cost_per_m: 0.59, output_cost_per_m: 0.79, supports_tools: true, supports_streaming: true },
  "xai/grok-4": { kind: "chat", context_window: 256_000, input_cost_per_m: 3, output_cost_per_m: 15, supports_tools: true, supports_streaming: true },
  "minimax/abab6.5s-chat": { kind: "chat", context_window: 245_000, input_cost_per_m: 0.2, output_cost_per_m: 0.2, supports_tools: true, supports_streaming: true },
  // Chat — Ollama (local)
  "ollama_chat/gemma3:e4b": { kind: "chat", context_window: 128_000, input_cost_per_m: 0, output_cost_per_m: 0, supports_tools: false, supports_vision: true, supports_streaming: true, local: true },
  "ollama_chat/qwen2.5:14b": { kind: "chat", context_window: 128_000, input_cost_per_m: 0, output_cost_per_m: 0, supports_tools: true, supports_streaming: true, local: true },
  "ollama_chat/qwen2.5-coder:14b": { kind: "chat", context_window: 32_000, input_cost_per_m: 0, output_cost_per_m: 0, supports_tools: true, supports_streaming: true, local: true },
  "ollama_chat/llama3.3": { kind: "chat", context_window: 128_000, input_cost_per_m: 0, output_cost_per_m: 0, supports_tools: true, supports_streaming: true, local: true },
  "ollama_chat/phi4": { kind: "chat", context_window: 16_000, input_cost_per_m: 0, output_cost_per_m: 0, supports_tools: true, supports_streaming: true, local: true },
  "ollama_chat/llama3.2-vision": { kind: "chat", context_window: 128_000, input_cost_per_m: 0, output_cost_per_m: 0, supports_tools: false, supports_vision: true, supports_streaming: true, local: true },
  // Embedding
  "jina_ai/jina-embeddings-v4": { kind: "embedding", cost_per_m: 0.18, local: false },
  "openai/text-embedding-3-large": { kind: "embedding", cost_per_m: 0.13, local: false },
  "openai/text-embedding-3-small": { kind: "embedding", cost_per_m: 0.02, local: false },
  "gemini/gemini-embedding-001": { kind: "embedding", cost_per_m: 0.025, local: false },
  "voyage/voyage-3-large": { kind: "embedding", cost_per_m: 0.18, local: false },
  "ollama/nomic-embed-text": { kind: "embedding", cost_per_m: 0, local: true },
};

/** Heuristic for models absent from KNOWN_MODELS (port of capability_infer.py). */
export function inferCapabilities(modelId: string): {
  supports_tools: boolean;
  supports_vision: boolean;
  supports_audio: boolean;
  local: boolean;
} {
  const s = modelId.toLowerCase();
  return {
    // PR-μ.1: keep this in sync with ``src/beever_atlas/llm/capability_infer.py``.
    // The substring rules are intentionally conservative — operators can pick
    // anything (UI no longer gates per PR-μ), this only colors the capability
    // badge informationally. False negatives are fine; we just under-claim.
    supports_tools:
      s.includes("gpt-") || s.includes("claude") || s.includes("gemini") || s.includes("mistral") ||
      s.includes("qwen") || s.includes("llama-3") ||
      s.includes("llama3.1") || s.includes("llama3.2") || s.includes("llama3.3") ||
      s.includes("minimax") || s.includes("deepseek-chat") || s.includes("grok") ||
      s.includes("firefunction") || s.includes("nous-hermes2") ||
      s.includes("glm-4") || s.includes("chatglm"),
    supports_vision:
      s.includes("vision") || s.includes("-vl") || s.includes("gpt-4o") || s.includes("gpt-4.1") ||
      s.includes("claude") || s.includes("gemini") || s.includes("llava"),
    supports_audio: s.includes("audio") || s.includes("whisper") || s.includes("gemini"),
    local: s.startsWith("ollama") || s.includes("lmstudio") || s.includes("vllm") || s.includes("localhost"),
  };
}

/**
 * Resolve the capability flags for a (provider, model) pair. Uses the catalog
 * first, falls back to inference. Returns the spec-shaped flags.
 */
export function resolveModelFlags(providerPrefix: string, model: string): {
  supports_tools: boolean;
  supports_vision: boolean;
  supports_audio: boolean;
  local: boolean;
} {
  const id = model.includes("/") ? model : `${providerPrefix}/${model}`;
  const spec = KNOWN_MODELS[id];
  if (spec) {
    return {
      supports_tools: spec.supports_tools ?? false,
      supports_vision: spec.supports_vision ?? false,
      supports_audio: spec.supports_audio ?? false,
      local: spec.local ?? false,
    };
  }
  return inferCapabilities(id);
}

/** Returns true iff the (provider, model) pair satisfies every required capability. */
export function isCompatible(
  providerPrefix: string,
  model: string,
  requiredCapabilities: string[],
): boolean {
  if (requiredCapabilities.length === 0) return true;
  const flags = resolveModelFlags(providerPrefix, model);
  const keyMap: Record<string, keyof typeof flags | null> = {
    tools: "supports_tools",
    vision: "supports_vision",
    audio: "supports_audio",
    "structured-output": null, // not gated yet — always passes
  };
  return requiredCapabilities.every((cap) => {
    const key = keyMap[cap];
    if (key == null) return true;
    return flags[key] === true;
  });
}

export function costHintForModel(providerPrefix: string, model: string): string {
  const id = model.includes("/") ? model : `${providerPrefix}/${model}`;
  const spec = KNOWN_MODELS[id];
  if (!spec) return "cost unknown";
  if (spec.kind === "embedding" || typeof spec.cost_per_m === "number") {
    return spec.cost_per_m === 0 ? "free (local)" : `~$${spec.cost_per_m?.toFixed(2)}/M`;
  }
  if (spec.input_cost_per_m != null) {
    if (spec.input_cost_per_m === 0) return "free (local)";
    return `~$${spec.input_cost_per_m.toFixed(2)}/M in · $${spec.output_cost_per_m?.toFixed(2)}/M out`;
  }
  return "—";
}

// ── cost rollup (one-card summary for the Agent-models tab) ─────────────────

/** A minimal endpoint shape — just what the rollup needs to find a provider. */
export interface RollupEndpoint {
  id: string;
  preset: string;
}

/** A minimal assignment shape — consumer + endpoint + model. */
export interface RollupAssignment {
  consumer: string;
  endpoint_id: string;
  model: string;
}

export interface CostBucket {
  /** Friendly model label (the bare model name). */
  label: string;
  /** Number of consumers on this model. */
  count: number;
  /** Human-readable input-rate string ("~$0.30/M in", "free", "—"). */
  inRate: string;
  /** Sortable input cost per million (0 for local, Infinity-ish unknown handled separately). */
  inCostPerM: number | null;
}

export interface CostRollup {
  /** One bucket per distinct model in use, sorted most-expensive first. */
  buckets: CostBucket[];
  /** The single most expensive assignment, or null if nothing priced. */
  mostExpensive: { consumer: string; model: string; rate: string } | null;
  /** How many in-use models have no price in KNOWN_MODELS. */
  unknownCount: number;
  /** True when there are no assignments at all. */
  empty: boolean;
}

/** TS mirror of llm/endpoints.preset_to_provider — used to qualify bare model names. */
function presetToProviderPrefix(preset: string): string {
  return (
    {
      google_ai: "gemini",
      ollama: "ollama",
      vllm: "openai",
      lmstudio: "openai",
      openrouter: "openai",
      litellm_proxy: "openai",
      custom: "openai",
    } as Record<string, string>
  )[preset] ?? preset;
}

function inputCostPerM(spec: ModelSpec | undefined): number | null {
  if (!spec) return null;
  if (typeof spec.cost_per_m === "number") return spec.cost_per_m;
  if (typeof spec.input_cost_per_m === "number") return spec.input_cost_per_m;
  return null;
}

function rateLabel(cost: number | null): string {
  if (cost == null) return "—";
  if (cost === 0) return "free";
  return `~$${cost.toFixed(2)}/M in`;
}

/**
 * Aggregate the per-consumer assignments into a single cost summary so the
 * Agent-models tab can show one rollup card instead of repeating the per-row
 * cost hint 16×. Pure — computes only from KNOWN_MODELS.
 */
export function costRollup(
  assignments: RollupAssignment[],
  endpointById: Record<string, RollupEndpoint>,
): CostRollup {
  if (assignments.length === 0) {
    return { buckets: [], mostExpensive: null, unknownCount: 0, empty: true };
  }
  const byModel = new Map<string, { label: string; count: number; cost: number | null }>();
  let unknownCount = 0;
  let mostExpensive: { consumer: string; model: string; cost: number } | null = null;

  for (const a of assignments) {
    const ep = endpointById[a.endpoint_id];
    const prefix = ep ? presetToProviderPrefix(ep.preset) : "";
    const id = a.model.includes("/") ? a.model : `${prefix}/${a.model}`;
    const spec = KNOWN_MODELS[id];
    const cost = inputCostPerM(spec);
    if (cost == null) unknownCount += 1;
    // Bucket key — bare model name keeps the label readable.
    const bareModel = a.model.includes("/") ? (a.model.split("/").pop() ?? a.model) : a.model;
    const existing = byModel.get(a.model);
    if (existing) {
      existing.count += 1;
    } else {
      byModel.set(a.model, { label: bareModel, count: 1, cost });
    }
    if (cost != null && cost > 0 && (mostExpensive == null || cost > mostExpensive.cost)) {
      mostExpensive = { consumer: a.consumer, model: a.model, cost };
    }
  }

  const buckets: CostBucket[] = Array.from(byModel.values())
    .map((b) => ({ label: b.label, count: b.count, inRate: rateLabel(b.cost), inCostPerM: b.cost }))
    .sort((x, y) => {
      // Most-expensive first; unknown (null) sinks to the bottom; ties → count desc.
      const xc = x.inCostPerM == null ? -1 : x.inCostPerM;
      const yc = y.inCostPerM == null ? -1 : y.inCostPerM;
      if (yc !== xc) return yc - xc;
      return y.count - x.count;
    });

  return {
    buckets,
    mostExpensive: mostExpensive
      ? { consumer: mostExpensive.consumer, model: mostExpensive.model, rate: rateLabel(mostExpensive.cost) }
      : null,
    unknownCount,
    empty: false,
  };
}
