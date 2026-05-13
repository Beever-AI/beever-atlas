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
  /** PR-λ.2: latest dispatch the recorder saw for this consumer (or null). */
  lastCall?: {
    ts: string;
    model: string;
    latency_ms: number | null;
    ok: boolean;
    response_model: string | null;
    error_class: string | null;
  } | null;
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


/** Tiny relative-time formatter — duplicates the hook's helper so this
 * module doesn't import the hook (avoids a circular ref through Storybook). */
function relativeTime(ts: string, now: Date = new Date()): string {
  const delta = (now.getTime() - new Date(ts).getTime()) / 1000;
  if (Number.isNaN(delta)) return "";
  if (delta < 5) return "just now";
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  const mins = Math.floor(delta / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
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

/**
 * PR-γ: narrow an endpoint's ``models`` list to those that classify as chat.
 * Pre-PR-α endpoints (no ``model_kinds``) keep the full list so the operator
 * isn't stranded until they re-Discover. Operator-typed entries that aren't in
 * ``model_kinds`` (e.g. a freshly added model that hasn't gone through the
 * classifier) also stay visible.
 */
function chatModelsFor(ep: Endpoint): string[] {
  const kinds = ep.model_kinds;
  if (!kinds || Object.keys(kinds).length === 0) return ep.models;
  return ep.models.filter((m) => kinds[m] === "chat" || !(m in kinds));
}

export function AgentAssignmentRow({
  consumer,
  assignment: a,
  endpoints,
  required,
  // PR-μ: ``suggested`` is part of the prop interface (callers still pass
  // it from the 422 catch-handler in case the API ever returns one), but
  // the component no longer renders the red-banner+suggested-fix surface.
  // Destructuring it would trip ``--noUnusedParameters``; ignore it here.
  lastCall,
  onUpsert,
  onToast,
  shouldToastSave,
}: AgentAssignmentRowProps) {
  const meta = metaForConsumer(consumer);
  // PR-ι: agent assignments are chat-only. Hide endpoints that can't serve
  // chat (Jina/Voyage presets, ``role === "embedding"`` operator-declared,
  // or classifier ran with zero chat-tagged models).
  const chatCapableEndpoints = endpoints.filter((e) => {
    if (e.preset === "jina_ai" || e.preset === "voyage") return false;
    if (e.role === "embedding") return false;
    const kinds = e.model_kinds;
    if (
      kinds &&
      Object.keys(kinds).length > 0 &&
      !Object.values(kinds).some((k) => k === "chat")
    ) {
      return false;
    }
    return true;
  });
  const endpointById = Object.fromEntries(endpoints.map((e) => [e.id, e]));
  const currentEp = a ? endpointById[a.endpoint_id] : undefined;
  const provPrefix = currentEp ? presetToProvider(currentEp.preset) : "";
  const compat = a && currentEp ? isCompatible(provPrefix, a.model, required) : true;
  const chatModels = currentEp ? chatModelsFor(currentEp) : [];

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
      let msg = e?.message ?? `Failed to save ${consumer}`;
      if (detail && typeof detail === "object") {
        // PR-λ.3: translate machine error codes into operator-readable
        // toasts. The backend's ``error`` field is for programmatic clients;
        // operators see this string in a toast and need to know what to do
        // about it.
        if (detail.error === "incompatible_assignment") {
          const missing: string[] = Array.isArray(detail.missing_capabilities)
            ? detail.missing_capabilities
            : [];
          const model = String(detail.model ?? "this model");
          const caps = missing.length > 0 ? missing.join(", ") : "required capabilities";
          msg = `${model} doesn't support ${caps} — required for ${consumer}. Pick a model that does (see "Suggested" below the row).`;
        } else if (typeof detail.error === "string") {
          msg = String(detail.error);
        }
      }
      onToast?.(msg, "error");
    }
  }

  function changeEndpoint(newEpId: string) {
    const newEp = endpointById[newEpId];
    if (!newEpId || !newEp) return;
    // Prefer a chat-classified model when the endpoint has classifications;
    // otherwise fall back to the first listed model (pre-α behaviour).
    const newChat = chatModelsFor(newEp);
    const firstModel = newChat[0] ?? newEp.models[0] ?? a?.model ?? "";
    if (!firstModel) {
      onToast?.(`${newEp.name} has no chat models — run Discover first`, "error");
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

  // PR-μ: ``suggested`` from a 422 response no longer reaches us (saves
  // are force=true now); the suggested-fix button was only ever shown
  // alongside the removed red banner.

  return (
    <div className="group hover:bg-muted/30 transition-colors">
      <div className="flex items-center gap-3 py-3 px-4">
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
          {/* PR-λ.2: live indicator of the most recent dispatch for this
              consumer. Helps operators confirm a model switch is actually
              in effect without reading server logs. */}
          {lastCall && (
            <div
              className={`text-[11px] mt-0.5 truncate ${
                lastCall.ok
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-destructive"
              }`}
              title={lastCall.ok ? "Last call OK" : `Last call failed: ${lastCall.error_class}`}
            >
              {lastCall.ok ? "✓ last call: " : "✗ last call failed: "}
              <span className="font-mono">{lastCall.model}</span>
              {lastCall.latency_ms != null && (
                <span className="text-muted-foreground"> · {lastCall.latency_ms}ms</span>
              )}
              <span className="text-muted-foreground"> · {relativeTime(lastCall.ts)}</span>
              {lastCall.response_model && lastCall.response_model !== lastCall.model && (
                <span
                  className="ml-1 text-amber-600 dark:text-amber-400"
                  title={`Provider echoed a different model: ${lastCall.response_model}`}
                >
                  · echoed {lastCall.response_model}
                </span>
              )}
            </div>
          )}
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
            {chatCapableEndpoints.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
              </option>
            ))}
          </select>
          {currentEp && (
            <select
              aria-label={`${consumer} model`}
              value={a?.model ?? ""}
              onChange={(e) => changeModel(e.target.value)}
              className="text-xs bg-background border border-border rounded-md px-2 py-1.5 min-w-[160px] text-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 hover:border-primary/40 transition-colors"
            >
              {chatModels.length === 0 && (
                <option value="">(no chat models — run Discover)</option>
              )}
              {/* PR-γ: when the saved model isn't in the chat-filtered set (e.g.
                  a stale embedding model still pinned by an old Assignment),
                  keep it as a visible option so the <select> isn't blank. */}
              {a?.model && !chatModels.includes(a.model) && (
                <option key={`__saved__${a.model}`} value={a.model}>
                  {a.model}
                </option>
              )}
              {chatModels.map((m) => (
                // PR-μ: capability info is INFORMATIONAL (see the badge
                // tooltips on the row), not a gate. Atlas's classifier is
                // substring-based and structurally cannot enumerate every
                // model in the world — disabling options creates more
                // false-positive lockouts than it prevents wrong choices.
                // The runtime "Last call" indicator (PR-λ.2) is now the
                // authoritative source of "does this combo actually work".
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
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

      {/* PR-μ: red "incompatible" banner removed. The substring-based
          classifier produces too many false positives (the operator can
          set ANY model name; we can't enumerate the universe). Truth
          source is now the "Last call" indicator above — a real failed
          call shows red there with a credential-safe error string. */}

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
            {chatCapableEndpoints
              .filter((e) => e.id !== a.endpoint_id)
              .map((e) => (
                <option key={e.id} value={e.id}>{e.name}</option>
              ))}
          </select>
        </div>
      )}
    </div>
  );
}
