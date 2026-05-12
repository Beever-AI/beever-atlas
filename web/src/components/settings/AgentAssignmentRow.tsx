import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  Cloud,
  Eye,
  HardDrive,
  Hammer,
  Mic,
  RotateCcw,
  Settings2,
} from "lucide-react";
import type { Assignment, Endpoint } from "@/lib/aiSetup";
import { isCompatible } from "@/lib/knownModels";
import { metaForConsumer } from "@/lib/agentMeta";

/** Endpoint preset key → LiteLLM provider prefix (TS mirror of llm/endpoints.preset_to_provider). */
function presetToProvider(preset: string): string {
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

const CAPABILITY_ICON: Record<string, { Icon: typeof Hammer; label: string }> = {
  tools: { Icon: Hammer, label: "needs tool-calling" },
  vision: { Icon: Eye, label: "needs vision" },
  audio: { Icon: Mic, label: "needs audio" },
};

export interface AgentAssignmentRowProps {
  consumer: string;
  assignment: Assignment | undefined;
  endpoints: Endpoint[];
  /** Required capability tokens for this consumer (from useAssignments.capabilities). */
  required: string[];
  /** Optional list of suggested fixes (model names) surfaced after an incompatible save. */
  suggested?: string[];
  /** Per-consumer upsert. Returns the saved Assignment (or throws). */
  onUpsert: (
    consumer: string,
    req: {
      endpoint_id: string;
      model: string;
      temperature?: number | null;
      max_tokens?: number | null;
      response_format?: "text" | "json" | null;
      fallback_endpoint_id?: string | null;
    },
  ) => Promise<Assignment>;
  /** Show a toast (used for the first save in an auto-save burst). */
  onToast?: (message: string, variant?: "info" | "error") => void;
  /** Returns true if this is the first save in the current burst (so only one toast fires). */
  shouldToastSave?: () => boolean;
}

function ProviderBadge({ preset }: { preset: string | undefined }) {
  const isLocal = preset === "ollama" || preset === "lmstudio" || preset === "vllm";
  if (isLocal) {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded bg-violet-500/10 text-violet-600 dark:text-violet-400 border border-violet-500/20">
        <HardDrive className="w-2.5 h-2.5" />
        Local
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded bg-sky-500/10 text-sky-600 dark:text-sky-400 border border-sky-500/20">
      <Cloud className="w-2.5 h-2.5" />
      Cloud
    </span>
  );
}

export function AgentAssignmentRow({
  consumer,
  assignment: a,
  endpoints,
  required,
  suggested,
  onUpsert,
  onToast,
  shouldToastSave,
}: AgentAssignmentRowProps) {
  const meta = metaForConsumer(consumer);
  const endpointById = Object.fromEntries(endpoints.map((e) => [e.id, e]));
  const currentEp = a ? endpointById[a.endpoint_id] : undefined;
  const provPrefix = currentEp ? presetToProvider(currentEp.preset) : "";
  const compat = a && currentEp ? isCompatible(provPrefix, a.model, required) : true;

  const hasCustomParams =
    a != null &&
    (a.temperature != null || a.max_tokens != null || a.response_format != null || a.fallback_endpoint_id != null);

  const [expanded, setExpanded] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);
  const flashTimer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (flashTimer.current != null) window.clearTimeout(flashTimer.current);
    };
  }, []);

  function flashSaved() {
    setSavedFlash(true);
    if (flashTimer.current != null) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setSavedFlash(false), 1000);
  }

  async function save(req: {
    endpoint_id: string;
    model: string;
    temperature?: number | null;
    max_tokens?: number | null;
    response_format?: "text" | "json" | null;
    fallback_endpoint_id?: string | null;
  }) {
    try {
      await onUpsert(consumer, req);
      flashSaved();
      if (onToast && (shouldToastSave ? shouldToastSave() : true)) {
        onToast(`${consumer} → ${req.model} saved`);
      }
    } catch (e: any) {
      const detail = e?.detail;
      const msg =
        detail && typeof detail === "object" && typeof detail.error === "string"
          ? String(detail.error)
          : (e?.message ?? `Failed to save ${consumer}`);
      onToast?.(msg, "error");
    }
  }

  function changeEndpoint(newEpId: string) {
    const newEp = endpointById[newEpId];
    if (!newEpId || !newEp) return;
    const firstModel = newEp.models[0] ?? a?.model ?? "";
    if (!firstModel) {
      onToast?.(`${newEp.name} has no models — run Discover first`, "error");
      return;
    }
    void save({ endpoint_id: newEpId, model: firstModel });
  }

  function changeModel(model: string) {
    if (!currentEp || !model) return;
    void save({ endpoint_id: currentEp.id, model });
  }

  function resetOverrides() {
    if (!a) return;
    void save({
      endpoint_id: a.endpoint_id,
      model: a.model,
      temperature: null,
      max_tokens: null,
      response_format: null,
      fallback_endpoint_id: null,
    });
  }

  const suggestedFix = suggested && suggested.length > 0 ? suggested[0] : null;

  return (
    <div className="group rounded-lg hover:bg-muted/30 transition-colors">
      <div className="flex items-center gap-3 py-2.5 px-3">
        {/* Left: name + description + pills */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium text-foreground truncate">{meta.displayName}</span>
            {hasCustomParams && (
              <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">
                Custom
              </span>
            )}
            {a && <ProviderBadge preset={currentEp?.preset} />}
          </div>
          {meta.description && <div className="text-xs text-muted-foreground truncate">{meta.description}</div>}
        </div>

        {/* Middle: endpoint + model selects */}
        <div className="flex items-center gap-2 shrink-0">
          <select
            aria-label={`${consumer} endpoint`}
            value={a?.endpoint_id ?? ""}
            onChange={(e) => changeEndpoint(e.target.value)}
            className="text-xs bg-background border border-border rounded-md px-2 py-1.5 text-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 hover:border-primary/40 transition-colors"
          >
            <option value="">— pick endpoint —</option>
            {endpoints.map((e) => (
              <option key={e.id} value={e.id}>{e.name}</option>
            ))}
          </select>
          {currentEp && (
            <select
              aria-label={`${consumer} model`}
              value={a?.model ?? ""}
              onChange={(e) => changeModel(e.target.value)}
              className="text-xs bg-background border border-border rounded-md px-2 py-1.5 min-w-[160px] text-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 hover:border-primary/40 transition-colors"
            >
              {currentEp.models.length === 0 && <option value="">(no models — run Discover)</option>}
              {currentEp.models.map((m) => {
                const ok = isCompatible(provPrefix, m, required);
                return (
                  <option key={m} value={m} disabled={!ok}>
                    {m}{ok ? "" : " (incompatible)"}
                  </option>
                );
              })}
            </select>
          )}

          {/* saved ✓ micro-flash */}
          {savedFlash && (
            <span className="inline-flex items-center text-green-600 dark:text-green-400" aria-label="saved" title="saved">
              <Check className="w-3.5 h-3.5" />
            </span>
          )}

          {/* Capability badges */}
          {required.map((cap) => {
            const capMeta = CAPABILITY_ICON[cap];
            if (!capMeta) return null;
            return (
              <span
                key={cap}
                title={
                  compat
                    ? capMeta.label
                    : `Needs ${cap === "tools" ? "tool-calling" : cap}; this model doesn't support it`
                }
                className={compat ? "text-muted-foreground" : "text-destructive"}
                data-capability={cap}
                data-incompatible={!compat || undefined}
              >
                <capMeta.Icon className="h-3.5 w-3.5 inline" />
              </span>
            );
          })}

          {/* Advanced gear */}
          {a && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              title="Advanced parameters"
              className={`p-1 rounded-md hover:bg-muted ${hasCustomParams ? "text-primary" : "text-muted-foreground"}`}
            >
              {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <Settings2 className="h-3.5 w-3.5" />}
            </button>
          )}

          {/* Reset overrides */}
          <button
            type="button"
            onClick={resetOverrides}
            disabled={!hasCustomParams}
            title="Reset advanced overrides"
            className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted disabled:opacity-0 disabled:pointer-events-none transition-all"
          >
            <RotateCcw className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Incompatible-model banner + suggested fix */}
      {a && currentEp && !compat && (
        <div className="mx-3 mb-2 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive flex items-center gap-2">
          <span>Current model can't satisfy this agent's requirements.</span>
          {suggestedFix && (
            <button
              type="button"
              onClick={() => void save({ endpoint_id: a.endpoint_id, model: suggestedFix })}
              className="ml-auto rounded border border-destructive/40 px-2 py-0.5 font-medium hover:bg-destructive/15"
            >
              Use {suggestedFix}
            </button>
          )}
        </div>
      )}

      {/* Advanced params drawer */}
      {expanded && a && currentEp && (
        <div className="mx-3 mb-2 grid grid-cols-[7rem_9rem] gap-1.5 text-xs items-center bg-muted/20 rounded p-2 w-fit">
          <label className="text-muted-foreground">temperature</label>
          <input
            type="number"
            step="0.1"
            min="0"
            max="2"
            className="rounded border border-border bg-background px-1.5 py-0.5"
            defaultValue={a.temperature ?? ""}
            placeholder="(default)"
            onBlur={(e) => {
              const v = e.target.value === "" ? null : Number(e.target.value);
              void save({
                endpoint_id: a.endpoint_id,
                model: a.model,
                temperature: v,
                max_tokens: a.max_tokens,
                response_format: a.response_format,
                fallback_endpoint_id: a.fallback_endpoint_id,
              });
            }}
          />
          <label className="text-muted-foreground">max_tokens</label>
          <input
            type="number"
            min="1"
            className="rounded border border-border bg-background px-1.5 py-0.5"
            defaultValue={a.max_tokens ?? ""}
            placeholder="(default)"
            onBlur={(e) => {
              const v = e.target.value === "" ? null : Number(e.target.value);
              void save({
                endpoint_id: a.endpoint_id,
                model: a.model,
                temperature: a.temperature,
                max_tokens: v,
                response_format: a.response_format,
                fallback_endpoint_id: a.fallback_endpoint_id,
              });
            }}
          />
          <label className="text-muted-foreground">response_format</label>
          <select
            className="rounded border border-border bg-background px-1.5 py-0.5"
            value={a.response_format ?? ""}
            onChange={(e) => {
              const v = (e.target.value || null) as "text" | "json" | null;
              void save({
                endpoint_id: a.endpoint_id,
                model: a.model,
                temperature: a.temperature,
                max_tokens: a.max_tokens,
                response_format: v,
                fallback_endpoint_id: a.fallback_endpoint_id,
              });
            }}
          >
            <option value="">(default)</option>
            <option value="text">text</option>
            <option value="json">json</option>
          </select>
          <label className="text-muted-foreground">fallback</label>
          <select
            className="rounded border border-border bg-background px-1.5 py-0.5"
            value={a.fallback_endpoint_id ?? ""}
            onChange={(e) => {
              const v = e.target.value || null;
              void save({
                endpoint_id: a.endpoint_id,
                model: a.model,
                temperature: a.temperature,
                max_tokens: a.max_tokens,
                response_format: a.response_format,
                fallback_endpoint_id: v,
              });
            }}
          >
            <option value="">(none)</option>
            {endpoints.filter((e) => e.id !== a.endpoint_id).map((e) => (
              <option key={e.id} value={e.id}>{e.name}</option>
            ))}
          </select>
        </div>
      )}
    </div>
  );
}
