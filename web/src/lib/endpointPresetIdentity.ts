import {
  Boxes,
  Bot,
  HardDrive,
  Server,
  Settings2,
  Sparkles,
  Wind,
  type LucideIcon,
} from "lucide-react";

/**
 * Per-preset visual identity for the Endpoints catalog — a tinted icon-box
 * colour + a fitting ``lucide-react`` icon, keyed on the endpoint's ``preset``
 * string. Tasteful: a tinted box, not a rainbow. Shares the accent vocabulary
 * with ``AgentModelsTab``'s ``ACCENT_STYLES`` (emerald / sky / violet / amber /
 * slate).
 */
export interface PresetIdentity {
  Icon: LucideIcon;
  /** Tailwind classes for the icon box (bg + text colour, light & dark). */
  iconBox: string;
  /** Tailwind classes for the small "family" label chip. */
  chip: string;
  /** Human family label, e.g. "Google", "OpenAI", "Local". */
  family: string;
}

const GOOGLE: PresetIdentity = {
  Icon: Sparkles,
  iconBox: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  chip: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  family: "Google",
};
const OPENAI: PresetIdentity = {
  Icon: Bot,
  iconBox: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  chip: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  family: "OpenAI",
};
const ANTHROPIC: PresetIdentity = {
  Icon: Sparkles,
  iconBox: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  chip: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  family: "Anthropic",
};
const LOCAL: PresetIdentity = {
  Icon: HardDrive,
  iconBox: "bg-violet-500/10 text-violet-600 dark:text-violet-400",
  chip: "bg-violet-500/10 text-violet-700 dark:text-violet-300",
  family: "Local",
};
const EMBEDDING: PresetIdentity = {
  Icon: Boxes,
  iconBox: "bg-teal-500/10 text-teal-600 dark:text-teal-400",
  chip: "bg-teal-500/10 text-teal-700 dark:text-teal-300",
  family: "Embeddings",
};
const PROXY: PresetIdentity = {
  Icon: Server,
  iconBox: "bg-indigo-500/10 text-indigo-600 dark:text-indigo-400",
  chip: "bg-indigo-500/10 text-indigo-700 dark:text-indigo-300",
  family: "Proxy",
};
const FAST: PresetIdentity = {
  Icon: Wind,
  iconBox: "bg-rose-500/10 text-rose-600 dark:text-rose-400",
  chip: "bg-rose-500/10 text-rose-700 dark:text-rose-300",
  family: "Fast inference",
};
const NEUTRAL: PresetIdentity = {
  Icon: Settings2,
  iconBox: "bg-slate-500/10 text-slate-600 dark:text-slate-400",
  chip: "bg-slate-500/10 text-slate-700 dark:text-slate-300",
  family: "Custom",
};

const BY_PRESET: Record<string, PresetIdentity> = {
  google_ai: GOOGLE,
  gemini: GOOGLE,
  openai: OPENAI,
  anthropic: ANTHROPIC,
  claude: ANTHROPIC,
  mistral: { ...OPENAI, family: "Mistral" },
  deepseek: { ...PROXY, family: "DeepSeek" },
  groq: { ...FAST, family: "Groq" },
  xai: { ...NEUTRAL, family: "xAI" },
  minimax: { ...PROXY, family: "MiniMax" },
  voyage: EMBEDDING,
  jina_ai: EMBEDDING,
  ollama: LOCAL,
  vllm: { ...LOCAL, Icon: Server, family: "Local (vLLM)" },
  lmstudio: { ...LOCAL, Icon: Server, family: "Local (LM Studio)" },
  openrouter: PROXY,
  litellm_proxy: { ...PROXY, family: "LiteLLM proxy" },
  custom: NEUTRAL,
};

/** Resolve the {@link PresetIdentity} for an endpoint's ``preset`` string. */
export function getPresetIdentity(preset: string): PresetIdentity {
  if (BY_PRESET[preset]) return BY_PRESET[preset];
  const p = preset.toLowerCase();
  if (p.includes("gemini") || p.includes("google")) return GOOGLE;
  if (p.includes("openai") || p.includes("gpt")) return OPENAI;
  if (p.includes("claude") || p.includes("anthropic")) return ANTHROPIC;
  if (p.includes("ollama") || p.includes("vllm") || p.includes("lmstudio") || p.includes("local")) return LOCAL;
  if (p.includes("voyage") || p.includes("jina") || p.includes("embed")) return EMBEDDING;
  if (p.includes("proxy") || p.includes("router") || p.includes("litellm")) return PROXY;
  return NEUTRAL;
}
