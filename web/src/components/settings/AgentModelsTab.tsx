import { useCallback, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  BookOpen,
  ChevronDown,
  GitMerge,
  HelpCircle,
  Image as ImageIcon,
  Layers,
  MessageCircleQuestion,
  Scale,
  Search,
  Server,
  Sparkles,
  Wrench,
  X,
} from "lucide-react";
import { useEndpoints } from "@/hooks/useEndpoints";
import { useAssignments } from "@/hooks/useAssignments";
import { useRecentLLMCalls } from "@/hooks/useRecentLLMCalls";
import { useToast } from "@/hooks/useToast";
import { PRESET_LABELS, type Endpoint } from "@/lib/aiSetup";
import { costRollup } from "@/lib/knownModels";
import { AGENT_META, GROUP_LABELS, GROUP_ORDER, type AgentGroup } from "@/lib/agentMeta";
import { AgentAssignmentRow } from "./AgentAssignmentRow";
import { AddEndpointPanel } from "./AddEndpointPanel";
import { ToastViewport } from "./ToastViewport";

// ── preset card metadata ───────────────────────────────────────────────────

interface PresetCard {
  key: string;
  label: string;
  description: string;
  Icon: React.ComponentType<{ className?: string }>;
  accent: string;
}

const PRESET_DESCRIPTIONS: Record<string, string> = {
  "gemini-balanced": "Recommended default — Gemini 2.5 Flash everywhere, fast and cheap.",
  "openai-quality": "GPT-4-class quality across every agent. Higher cost.",
  "claude-quality-gemini-fast": "Claude for reasoning-heavy agents, Gemini Flash for the bulk.",
  "fully-local": "Ollama-only — no data leaves the box. Quality varies by model.",
};

const PRESET_ACCENT: Record<string, string> = {
  "gemini-balanced": "emerald",
  "openai-quality": "sky",
  "claude-quality-gemini-fast": "amber",
  "fully-local": "violet",
};

const PRESET_ICON: Record<string, React.ComponentType<{ className?: string }>> = {
  "gemini-balanced": Scale,
  "openai-quality": Sparkles,
  "claude-quality-gemini-fast": Wrench,
  "fully-local": Server,
};

const PRESET_CARDS: PresetCard[] = Object.entries(PRESET_LABELS)
  .filter(([key]) => key !== "custom")
  .map(([key, label]) => ({
    key,
    label,
    description: PRESET_DESCRIPTIONS[key] ?? "",
    Icon: PRESET_ICON[key] ?? Scale,
    accent: PRESET_ACCENT[key] ?? "emerald",
  }));

const ACCENT_STYLES: Record<string, { bg: string; border: string; text: string; iconBg: string }> = {
  emerald: { bg: "hover:bg-emerald-500/5", border: "hover:border-emerald-500/40", text: "text-emerald-600 dark:text-emerald-400", iconBg: "bg-emerald-500/10" },
  amber: { bg: "hover:bg-amber-500/5", border: "hover:border-amber-500/40", text: "text-amber-600 dark:text-amber-400", iconBg: "bg-amber-500/10" },
  sky: { bg: "hover:bg-sky-500/5", border: "hover:border-sky-500/40", text: "text-sky-600 dark:text-sky-400", iconBg: "bg-sky-500/10" },
  violet: { bg: "hover:bg-violet-500/5", border: "hover:border-violet-500/40", text: "text-violet-600 dark:text-violet-400", iconBg: "bg-violet-500/10" },
};

const GROUP_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  ingestion: Layers,
  media: ImageIcon,
  post_processing: GitMerge,
  wiki: BookOpen,
  qa: MessageCircleQuestion,
  utility: Wrench,
};

// ── help drawer ────────────────────────────────────────────────────────────

function HelpDrawer({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-40 bg-background/60 backdrop-blur-sm flex justify-end" onClick={onClose}>
      <div
        className="w-full max-w-md h-full bg-card border-l border-border overflow-y-auto p-5 space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">Picking chat models</h3>
          <button type="button" className="text-xs px-2 py-1 rounded-md hover:bg-muted" onClick={onClose}>
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="text-xs space-y-3 text-muted-foreground">
          <HelpSection title="Gemini balanced (recommended)">
            Gemini 2.5 Flash across the board — ~$0.30/M in, fast, tool-calling + vision.
            The best price/quality for most installs.
          </HelpSection>
          <HelpSection title="OpenAI quality">
            GPT-4-class models everywhere. Strongest general quality, ~10× the cost of Flash.
          </HelpSection>
          <HelpSection title="Claude + Gemini hybrid">
            Claude for the reasoning-heavy agents (Q&A, contradiction detection), Gemini Flash
            for the high-volume ingestion agents. A middle ground on cost.
          </HelpSection>
          <HelpSection title="Fully local (Ollama)">
            Nothing leaves the box. Quality varies a lot by model; some local models lack
            tool-calling or vision, which a few agents need — watch the red capability badges.
          </HelpSection>
          <p>Expand a group below to override any individual agent. Changes save automatically.</p>
        </div>
      </div>
    </div>
  );
}

function HelpSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-foreground font-medium mb-0.5">{title}</div>
      <div>{children}</div>
    </div>
  );
}

// ── component ──────────────────────────────────────────────────────────────

const AGENT_CONSUMERS = new Set(AGENT_META.map((a) => a.name));

export function AgentModelsTab() {
  const ep = useEndpoints();
  const asn = useAssignments();
  const recent = useRecentLLMCalls();
  const { toasts, show: showToast, dismiss: dismissToast } = useToast();

  const [search, setSearch] = useState("");
  const [showHelp, setShowHelp] = useState(false);
  const [showAddEndpoint, setShowAddEndpoint] = useState(false);
  const [presetError, setPresetError] = useState<{ provider: string | null; message: string } | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({
    ingestion: false,
    media: true,
    post_processing: true,
    wiki: true,
    qa: true,
    utility: true,
  });
  const [incompatibleSuggestions, setIncompatibleSuggestions] = useState<Record<string, string[]>>({});

  // First-in-burst toast gate for auto-saves.
  const burstActive = useRef(false);
  const burstTimer = useRef<number | null>(null);
  const shouldToastSave = useCallback(() => {
    if (burstActive.current) {
      if (burstTimer.current != null) window.clearTimeout(burstTimer.current);
      burstTimer.current = window.setTimeout(() => {
        burstActive.current = false;
      }, 1500);
      return false;
    }
    burstActive.current = true;
    if (burstTimer.current != null) window.clearTimeout(burstTimer.current);
    burstTimer.current = window.setTimeout(() => {
      burstActive.current = false;
    }, 1500);
    return true;
  }, []);

  const endpointById = useMemo(
    () => Object.fromEntries(ep.endpoints.map((e) => [e.id, e])) as Record<string, Endpoint>,
    [ep.endpoints],
  );
  const assignmentByConsumer = useMemo(
    () => Object.fromEntries(asn.assignments.map((a) => [a.consumer, a])),
    [asn.assignments],
  );

  // Source of truth: which agent consumers exist (defaultConsumers minus embedding),
  // intersected with what we have display copy for. Fall back to AGENT_META order
  // when defaultConsumers is empty (e.g. before the first fetch).
  const agentConsumers = useMemo(() => {
    const fromBackend = asn.defaultConsumers.filter((c) => c !== "embedding");
    const list = fromBackend.length > 0 ? fromBackend : AGENT_META.map((a) => a.name);
    // Keep only ones we know how to render; preserve a deterministic order via AGENT_META.
    const known = list.filter((c) => AGENT_CONSUMERS.has(c));
    const metaOrder = AGENT_META.map((a) => a.name);
    return known.sort((x, y) => metaOrder.indexOf(x) - metaOrder.indexOf(y));
  }, [asn.defaultConsumers]);

  const groupedConsumers = useMemo(() => {
    const byGroup: Record<string, string[]> = {};
    for (const c of agentConsumers) {
      const meta = AGENT_META.find((a) => a.name === c);
      const g = (meta?.group ?? "utility") as AgentGroup;
      (byGroup[g] ??= []).push(c);
    }
    return byGroup;
  }, [agentConsumers]);

  const rollup = useMemo(
    () => costRollup(asn.assignments.filter((a) => a.consumer !== "embedding"), endpointById),
    [asn.assignments, endpointById],
  );

  const searchLc = search.trim().toLowerCase();

  function consumerMatchesSearch(c: string): boolean {
    if (!searchLc) return true;
    const meta = AGENT_META.find((a) => a.name === c);
    return (
      c.toLowerCase().includes(searchLc) ||
      (meta?.displayName.toLowerCase().includes(searchLc) ?? false) ||
      (meta?.description.toLowerCase().includes(searchLc) ?? false)
    );
  }

  function toggleGroup(g: string) {
    setCollapsed((prev) => ({ ...prev, [g]: !prev[g] }));
  }

  // ── preset apply ──────────────────────────────────────────────────────
  async function handlePreset(key: string) {
    setPresetError(null);
    try {
      const result = await asn.applyPreset(key);
      await ep.refetch();
      const label = PRESET_LABELS[key] ?? key;
      const kept =
        result.preserved.length > 0
          ? `, ${result.preserved.length} kept custom params: ${result.preserved.join(", ")}`
          : "";
      showToast(`Applied '${label}' — ${result.diff.length} updated${kept}`);
    } catch (e: any) {
      const detail = e?.detail;
      const errCode = detail && typeof detail === "object" ? detail.error : undefined;
      if (errCode === "preset_requirements_not_met") {
        const provider = (detail?.provider as string | undefined) ?? null;
        const msg = provider
          ? `This preset needs a ${provider} endpoint first.`
          : "This preset needs an endpoint that isn't configured yet.";
        setPresetError({ provider, message: msg });
        showToast(msg, "error");
      } else {
        const msg =
          detail && typeof detail === "object" && typeof detail.error === "string"
            ? String(detail.error)
            : (asn.error ?? e?.message ?? "Failed to apply preset");
        showToast(msg, "error");
      }
    }
  }

  // Wrap upsert so we can stash a 422 incompatible suggestion against the consumer.
  const upsertWithCapture = useCallback(
    async (
      consumer: string,
      req: {
        endpoint_id: string;
        model: string;
        temperature?: number | null;
        max_tokens?: number | null;
        response_format?: "text" | "json" | null;
        fallback_endpoint_id?: string | null;
      },
    ) => {
      try {
        const saved = await asn.upsert(consumer, req);
        // Successful save clears any prior suggestion.
        setIncompatibleSuggestions((prev) => {
          if (!(consumer in prev)) return prev;
          const next = { ...prev };
          delete next[consumer];
          return next;
        });
        return saved;
      } catch (e: any) {
        const detail = e?.detail;
        if (detail && typeof detail === "object" && detail.error === "incompatible_assignment") {
          const suggested: string[] = Array.isArray(detail.suggested)
            ? detail.suggested.map((s: any) => (typeof s === "string" ? s : s?.model)).filter(Boolean)
            : [];
          if (suggested.length > 0) {
            setIncompatibleSuggestions((prev) => ({ ...prev, [consumer]: suggested }));
          }
        }
        throw e;
      }
    },
    [asn],
  );

  const isLoading = ep.isLoading || asn.isLoading;
  const noEndpoints = !ep.isLoading && ep.endpoints.length === 0;

  return (
    <div className="space-y-6">
      {/* ── Intro ─────────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm text-muted-foreground max-w-2xl">
          These agents power ingestion, media processing, the wiki, and Ask. Most people pick a
          preset; expand a group to override an individual agent — changes save automatically.
        </p>
        <button
          type="button"
          onClick={() => setShowHelp(true)}
          title="How presets work"
          aria-label="Help: picking chat models"
          className="shrink-0 p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted"
        >
          <HelpCircle className="h-4 w-4" />
        </button>
      </div>

      {asn.error && (
        <div className="text-xs text-destructive bg-destructive/10 rounded-md px-3 py-2">{asn.error}</div>
      )}

      {/* ── Preset cards ──────────────────────────────────────────────── */}
      <div>
        <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-3">Presets</div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {PRESET_CARDS.map(({ key, label, description, Icon, accent }) => {
            const style = ACCENT_STYLES[accent] ?? ACCENT_STYLES.emerald;
            return (
              <button
                key={key}
                type="button"
                onClick={() => handlePreset(key)}
                disabled={isLoading}
                className={`group flex flex-col items-start gap-2 p-3.5 rounded-xl border-2 border-border bg-card text-left transition-all ${style.border} ${style.bg} disabled:opacity-50`}
              >
                <div className={`w-8 h-8 rounded-lg ${style.iconBg} flex items-center justify-center ${style.text}`}>
                  <Icon className="h-4 w-4" />
                </div>
                <div>
                  <div className="text-sm font-medium text-foreground">{label}</div>
                  {description && <div className="text-xs text-muted-foreground mt-0.5">{description}</div>}
                </div>
              </button>
            );
          })}
        </div>
        {presetError && (
          <div className="mt-3 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive flex items-center gap-2 flex-wrap">
            <span>{presetError.message}</span>
            <button
              type="button"
              onClick={() => setShowAddEndpoint(true)}
              className="ml-auto rounded border border-destructive/40 px-2 py-0.5 font-medium hover:bg-destructive/15"
            >
              Add endpoint
            </button>
          </div>
        )}
      </div>

      {/* ── Cost summary ──────────────────────────────────────────────── */}
      {!rollup.empty && (
        <div className="rounded-lg border border-border bg-muted/20 px-4 py-3 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">Estimated cost mix: </span>
          {rollup.buckets.map((b, i) => (
            <span key={b.label}>
              {i > 0 && ", "}
              {b.count} {b.count === 1 ? "agent" : "agents"} on {b.label} ({b.inRate})
            </span>
          ))}
          {rollup.unknownCount > 0 && (
            <span>
              {rollup.buckets.length > 0 ? ", " : ""}
              {rollup.unknownCount} with unknown pricing
            </span>
          )}
          {rollup.mostExpensive && (
            <span className="block mt-0.5">
              Most expensive: {rollup.mostExpensive.consumer} → {rollup.mostExpensive.model} ({rollup.mostExpensive.rate})
            </span>
          )}
        </div>
      )}

      {/* ── Endpoint strip / empty state ──────────────────────────────── */}
      {noEndpoints ? (
        <div className="rounded-xl border-2 border-dashed border-border bg-card p-6 text-center space-y-3">
          <div className="text-sm font-medium text-foreground">Add your first endpoint to get started</div>
          <p className="text-xs text-muted-foreground max-w-md mx-auto">
            An endpoint is a model provider you've connected (an API base URL + key, or a local Ollama).
            Add one, or apply a preset to set everything up at once.
          </p>
          {!showAddEndpoint && (
            <button
              type="button"
              onClick={() => setShowAddEndpoint(true)}
              className="rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90"
            >
              Add endpoint
            </button>
          )}
          {showAddEndpoint && (
            <div className="text-left">
              <AddEndpointPanel
                onCancel={() => setShowAddEndpoint(false)}
                onCreate={async (req) => {
                  await ep.create(req);
                  setShowAddEndpoint(false);
                  showToast(`Endpoint '${req.name}' added`);
                }}
              />
            </div>
          )}
          <div className="flex flex-wrap items-center justify-center gap-1.5 pt-1">
            <span className="text-[11px] text-muted-foreground">…or apply a preset:</span>
            {PRESET_CARDS.map(({ key, label }) => (
              <button
                key={key}
                type="button"
                onClick={() => handlePreset(key)}
                disabled={isLoading}
                className="rounded-md border border-border px-2.5 py-1 text-xs font-medium hover:bg-accent disabled:opacity-50"
              >
                {label}
              </button>
            ))}
          </div>
          {presetError && (
            <div className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">{presetError.message}</div>
          )}
        </div>
      ) : (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            {ep.endpoints.length} {ep.endpoints.length === 1 ? "endpoint" : "endpoints"} configured
          </span>
          <Link to="/settings/endpoints" className="text-primary hover:underline">
            Manage
          </Link>
        </div>
      )}

      {/* Inline Add-endpoint panel (when triggered from the preset error and endpoints already exist) */}
      {showAddEndpoint && !noEndpoints && (
        <AddEndpointPanel
          onCancel={() => setShowAddEndpoint(false)}
          onCreate={async (req) => {
            await ep.create(req);
            setShowAddEndpoint(false);
            setPresetError(null);
            showToast(`Endpoint '${req.name}' added`);
          }}
        />
      )}

      {/* ── Search + agent groups ─────────────────────────────────────── */}
      {!noEndpoints && (
        <>
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground pointer-events-none" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter agents…"
              className="w-full text-sm bg-background border border-border rounded-lg pl-9 pr-3 py-2 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30"
            />
          </div>

          <div className="space-y-3">
            {GROUP_ORDER.map((group) => {
              const all = groupedConsumers[group] ?? [];
              if (all.length === 0) return null;
              const visible = all.filter(consumerMatchesSearch);
              if (visible.length === 0) return null;
              const GroupIcon = GROUP_ICONS[group] ?? Layers;
              const isCollapsed = (collapsed[group] ?? false) && !searchLc;
              const customCount = visible.filter((c) => {
                const a = assignmentByConsumer[c];
                return (
                  a != null &&
                  (a.temperature != null || a.max_tokens != null || a.response_format != null || a.fallback_endpoint_id != null)
                );
              }).length;
              return (
                <div key={group} className="rounded-xl border border-border bg-card overflow-hidden">
                  <button
                    type="button"
                    onClick={() => toggleGroup(group)}
                    className="w-full flex items-center justify-between px-4 py-3 hover:bg-muted/30 transition-colors"
                  >
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-lg bg-primary/10 text-primary flex items-center justify-center">
                        <GroupIcon className="w-4 h-4" />
                      </div>
                      <div className="text-left">
                        <div className="text-sm font-semibold text-foreground">{GROUP_LABELS[group] ?? group}</div>
                        <div className="text-xs text-muted-foreground">
                          {visible.length} agent{visible.length !== 1 ? "s" : ""}
                          {customCount > 0 && <span> · {customCount} customized</span>}
                        </div>
                      </div>
                    </div>
                    <ChevronDown
                      className={`w-4 h-4 text-muted-foreground transition-transform ${isCollapsed ? "-rotate-90" : ""}`}
                    />
                  </button>
                  {!isCollapsed && (
                    <div className="border-t border-border bg-background/40 divide-y divide-border/40">
                      {visible.map((c) => (
                        <AgentAssignmentRow
                          key={c}
                          consumer={c}
                          assignment={assignmentByConsumer[c]}
                          endpoints={ep.endpoints}
                          required={asn.capabilities[c] ?? []}
                          suggested={incompatibleSuggestions[c]}
                          lastCall={(() => {
                            // PR-λ.7: prefer consumer-tagged calls (dispatch
                            // wrappers) but fall back to (api_base, model)
                            // match. Most agent calls go via Google ADK's
                            // ``LiteLlm`` wrapper which the LiteLLM callback
                            // records without a consumer tag — we attribute
                            // them by matching the assignment's endpoint+model.
                            const direct = recent.lastForConsumer(c);
                            if (direct) return direct;
                            const a = assignmentByConsumer[c];
                            if (!a) return null;
                            const epRec = endpointById[a.endpoint_id];
                            return recent.lastByModel(epRec?.base_url, a.model);
                          })()}
                          onUpsert={upsertWithCapture}
                          onToast={showToast}
                          shouldToastSave={shouldToastSave}
                        />
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}

      {showHelp && <HelpDrawer onClose={() => setShowHelp(false)} />}
      <ToastViewport toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}
