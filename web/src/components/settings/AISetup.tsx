import { useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Eye,
  Hammer,
  KeyRound,
  Mic,
  Plus,
  PlugZap,
  Settings2,
  X,
} from "lucide-react";
import { useEndpoints } from "@/hooks/useEndpoints";
import { useAssignments } from "@/hooks/useAssignments";
import { useToast } from "@/hooks/useToast";
import { PRESET_LABELS, type Endpoint } from "@/lib/aiSetup";
import { costHintForModel, isCompatible } from "@/lib/knownModels";
import { EndpointCard, type EndpointDiscoverResult, type EndpointTestResult } from "./EndpointCard";
import { AddEndpointPanel } from "./AddEndpointPanel";
import { ToastViewport } from "./ToastViewport";

// ── helpers ────────────────────────────────────────────────────────────────

/** Endpoint preset key → LiteLLM provider prefix (TS mirror of llm/endpoints.preset_to_provider). */
function presetToProvider(preset: string): string {
  return (
    { google_ai: "gemini", ollama: "ollama", vllm: "openai", lmstudio: "openai", openrouter: "openai", litellm_proxy: "openai", custom: "openai" } as Record<string, string>
  )[preset] ?? preset;
}

// Consumers grouped for the Assignments list.
const CONSUMER_GROUPS: { label: string; consumers: string[] }[] = [
  { label: "Embedding", consumers: ["embedding"] },
  { label: "Ingestion agents", consumers: ["fact_extractor", "entity_extractor", "cross_batch_validator", "coreference_resolver", "contradiction_detector", "summarizer"] },
  { label: "Q&A agents", consumers: ["qa_agent", "qa_router"] },
  { label: "Wiki agents", consumers: ["wiki_compiler", "wiki_maintainer"] },
  { label: "Media agents", consumers: ["image_describer", "video_analyzer", "audio_transcriber", "document_digester"] },
  { label: "Other", consumers: ["echo", "csv_mapper"] },
];

const CAPABILITY_ICON: Record<string, { Icon: typeof Hammer; label: string }> = {
  tools: { Icon: Hammer, label: "needs tool-calling" },
  vision: { Icon: Eye, label: "needs vision" },
  audio: { Icon: Mic, label: "needs audio" },
};

// ── component ──────────────────────────────────────────────────────────────

export function AISetup() {
  const ep = useEndpoints();
  const asn = useAssignments();
  const { toasts, show: showToast, dismiss: dismissToast } = useToast();

  const [showAddEndpoint, setShowAddEndpoint] = useState(false);
  const [presetBanner, setPresetBanner] = useState<{ preset: string; preserved: string[]; changed: number } | null>(null);
  const [busyEndpointId, setBusyEndpointId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, EndpointTestResult>>({});
  const [discoverResults, setDiscoverResults] = useState<Record<string, EndpointDiscoverResult>>({});
  const [expandedConsumers, setExpandedConsumers] = useState<Set<string>>(new Set());

  function toggleExpanded(consumer: string) {
    setExpandedConsumers((prev) => {
      const next = new Set(prev);
      if (next.has(consumer)) next.delete(consumer);
      else next.add(consumer);
      return next;
    });
  }

  // Full per-Assignment upsert (used by the advanced-params drawer).
  async function handleAssignmentParams(
    consumer: string,
    endpointId: string,
    model: string,
    params: { temperature?: number | null; max_tokens?: number | null; response_format?: "text" | "json" | null; fallback_endpoint_id?: string | null },
  ) {
    try {
      await asn.upsert(consumer, { endpoint_id: endpointId, model, ...params });
    } catch {
      // hook stores the error
    }
  }

  const endpointById = useMemo(
    () => Object.fromEntries(ep.endpoints.map((e) => [e.id, e])) as Record<string, Endpoint>,
    [ep.endpoints],
  );

  const assignmentByConsumer = useMemo(
    () => Object.fromEntries(asn.assignments.map((a) => [a.consumer, a])),
    [asn.assignments],
  );

  // ── preset apply ──────────────────────────────────────────────────────
  async function handlePreset(preset: string) {
    try {
      const result = await asn.applyPreset(preset);
      setPresetBanner({ preset, preserved: result.preserved, changed: result.diff.length });
      await ep.refetch();
      const label = PRESET_LABELS[preset] ?? preset;
      const kept = result.preserved.length > 0 ? `, ${result.preserved.length} kept custom` : "";
      showToast(`Applied '${label}' — ${result.diff.length} updated${kept}`);
    } catch {
      // useAssignments stores the error (e.g. preset_requirements_not_met);
      // the error banner below surfaces it.
      showToast(asn.error ?? "Failed to apply preset", "error");
    }
  }

  // ── endpoint actions ──────────────────────────────────────────────────
  async function handleTest(id: string) {
    setBusyEndpointId(id);
    try {
      const r = await ep.test(id);
      setTestResults((prev) => ({ ...prev, [id]: r }));
      const name = endpointById[id]?.name ?? id;
      if (r.ok) showToast(`${name} connected · ${r.latency_ms}ms`);
      else showToast(`${name} test failed: ${r.error ?? "unknown"}`, "error");
    } finally {
      setBusyEndpointId(null);
      await ep.refetch();
    }
  }

  async function handleDiscover(id: string) {
    setBusyEndpointId(id);
    try {
      const r = await ep.discover(id);
      const name = endpointById[id]?.name ?? id;
      if (r.ok && r.models.length > 0) {
        await ep.update(id, { models: r.models });
        setDiscoverResults((prev) => ({ ...prev, [id]: { ok: true, count: r.models.length, error: null } }));
        showToast(`Found ${r.models.length} models — added to ${name}`);
      } else if (r.ok) {
        setDiscoverResults((prev) => ({ ...prev, [id]: { ok: true, count: 0, error: null } }));
        showToast(`No models discovered for ${name}`);
      } else {
        setDiscoverResults((prev) => ({ ...prev, [id]: { ok: false, count: 0, error: r.error } }));
        showToast(`Discover failed for ${name}: ${r.error ?? "unknown"}`, "error");
      }
    } finally {
      setBusyEndpointId(null);
    }
  }

  async function handleDeleteEndpoint(id: string) {
    if (!confirm("Delete this endpoint? Assignments referencing it will block the delete.")) return;
    setBusyEndpointId(id);
    try {
      await ep.remove(id);
      showToast("Endpoint deleted");
    } catch (e: any) {
      const detail = e?.body?.detail ?? e?.detail;
      if (detail?.error === "endpoint_in_use_as_primary_or_fallback") {
        const consumers = (detail.consumers ?? []).join(", ");
        showToast(`In use by: ${consumers}. Reassign those first.`, "error");
      } else {
        showToast(detail?.error ? String(detail.error) : (e?.message ?? "Failed to delete endpoint"), "error");
      }
    } finally {
      setBusyEndpointId(null);
    }
  }

  // ── assignment change ─────────────────────────────────────────────────
  async function handleAssignmentChange(consumer: string, endpointId: string, model: string) {
    try {
      await asn.upsert(consumer, { endpoint_id: endpointId, model });
      showToast(`${consumer} → ${model} saved`);
    } catch {
      // hook stores the error (incompatible_assignment 422 etc.)
      showToast(asn.error ?? `Failed to save ${consumer}`, "error");
    }
  }

  const isLoading = ep.isLoading || asn.isLoading;

  return (
    <div className="space-y-6">
      {/* ── Quick Start presets ──────────────────────────────────────── */}
      <section className="rounded-lg border border-border bg-card p-4">
        <div className="flex items-center gap-2 mb-2">
          <PlugZap className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium">Quick start</h3>
        </div>
        <p className="text-xs text-muted-foreground mb-3">
          Pick a preset to seed every agent + embedding assignment. You can refine individual agents below.
        </p>
        <div className="flex flex-wrap gap-2">
          {Object.entries(PRESET_LABELS).map(([key, label]) => (
            <button
              key={key}
              onClick={() => handlePreset(key)}
              disabled={key === "custom" || isLoading}
              className="rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {label}
            </button>
          ))}
        </div>
        {presetBanner && (
          <div className="mt-3 rounded-md bg-muted/50 px-3 py-2 text-xs flex items-start gap-2">
            <CheckCircle2 className="h-3.5 w-3.5 text-green-600 mt-0.5 shrink-0" />
            <span>
              Applied <strong>{PRESET_LABELS[presetBanner.preset] ?? presetBanner.preset}</strong> to {presetBanner.changed} assignments.
              {presetBanner.preserved.length > 0 && (
                <> {presetBanner.preserved.length} kept their custom params: {presetBanner.preserved.join(", ")}.</>
              )}
            </span>
            <button onClick={() => setPresetBanner(null)} className="ml-auto text-muted-foreground hover:text-foreground">
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
        {asn.error && (
          <div className="mt-3 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive flex items-center gap-2">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            {asn.error}
          </div>
        )}
      </section>

      {/* ── Endpoints ────────────────────────────────────────────────── */}
      <section className="rounded-lg border border-border bg-card p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-muted-foreground" />
            <h3 className="text-sm font-medium">Endpoints</h3>
          </div>
          <button
            onClick={() => setShowAddEndpoint((v) => !v)}
            className="rounded-md border border-border px-2.5 py-1 text-xs font-medium hover:bg-accent flex items-center gap-1"
          >
            <Plus className="h-3 w-3" /> Add endpoint
          </button>
        </div>

        {showAddEndpoint && (
          <div className="mb-3">
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

        {ep.endpoints.length === 0 && !showAddEndpoint && (
          <p className="text-xs text-muted-foreground py-2">
            No endpoints configured yet. Add one above, or seed via the Quick Start presets.
          </p>
        )}

        <div className="space-y-3">
          {ep.endpoints.map((e) => {
            const usedBy = asn.assignments.filter((a) => a.endpoint_id === e.id || a.fallback_endpoint_id === e.id);
            return (
              <EndpointCard
                key={e.id}
                endpoint={e}
                usedByCount={usedBy.length}
                busy={busyEndpointId === e.id}
                testResult={testResults[e.id] ?? null}
                discoverResult={discoverResults[e.id] ?? null}
                onTest={() => handleTest(e.id)}
                onDiscover={() => handleDiscover(e.id)}
                onDelete={() => handleDeleteEndpoint(e.id)}
              />
            );
          })}
        </div>
        {ep.error && (
          <div className="mt-3 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive flex items-center gap-2">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" /> {ep.error}
          </div>
        )}
      </section>

      {/* ── Assignments ──────────────────────────────────────────────── */}
      <section className="rounded-lg border border-border bg-card p-4">
        <div className="flex items-center gap-2 mb-3">
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium">Assignments</h3>
        </div>
        {ep.endpoints.length === 0 ? (
          <p className="text-xs text-muted-foreground py-2">Add an endpoint first, then assign agents to it.</p>
        ) : (
          <div className="space-y-4">
            {CONSUMER_GROUPS.map((group) => (
              <div key={group.label}>
                <div className="text-[11px] uppercase tracking-wide text-muted-foreground mb-1">{group.label}</div>
                <div className="space-y-1">
                  {group.consumers.map((consumer) => {
                    const a = assignmentByConsumer[consumer];
                    const required = asn.capabilities[consumer] ?? [];
                    const currentEp = a ? endpointById[a.endpoint_id] : undefined;
                    const provPrefix = currentEp ? presetToProvider(currentEp.preset) : "";
                    const compat = a && currentEp ? isCompatible(provPrefix, a.model, required) : true;
                    const expanded = expandedConsumers.has(consumer);
                    const hasCustomParams =
                      a != null &&
                      (a.temperature != null || a.max_tokens != null || a.response_format != null || a.fallback_endpoint_id != null);
                    return (
                      <div key={consumer}>
                        <div className="flex items-center gap-2 text-xs py-0.5">
                          <span className="w-44 shrink-0 font-mono">{consumer}</span>
                          {/* Endpoint picker */}
                          <select
                            className="rounded border border-border bg-background px-1.5 py-0.5"
                            value={a?.endpoint_id ?? ""}
                            onChange={(e) => {
                              const newEpId = e.target.value;
                              const newEp = endpointById[newEpId];
                              const firstModel = newEp?.models[0] ?? a?.model ?? "";
                              if (newEpId && firstModel) handleAssignmentChange(consumer, newEpId, firstModel);
                            }}
                          >
                            <option value="">— pick endpoint —</option>
                            {ep.endpoints.map((e) => (
                              <option key={e.id} value={e.id}>{e.name}</option>
                            ))}
                          </select>
                          {/* Model picker */}
                          {currentEp && (
                            <select
                              className="rounded border border-border bg-background px-1.5 py-0.5"
                              value={a?.model ?? ""}
                              onChange={(e) => handleAssignmentChange(consumer, currentEp.id, e.target.value)}
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
                          {/* Capability badges */}
                          {required.map((cap) => {
                            const meta = CAPABILITY_ICON[cap];
                            if (!meta) return null;
                            return (
                              <span
                                key={cap}
                                title={compat ? meta.label : `${meta.label} — current model is incompatible`}
                                className={compat ? "text-muted-foreground" : "text-destructive"}
                              >
                                <meta.Icon className="h-3 w-3 inline" />
                              </span>
                            );
                          })}
                          {/* Advanced-params toggle */}
                          {a && (
                            <button
                              onClick={() => toggleExpanded(consumer)}
                              title="Advanced parameters"
                              className={`rounded px-1 py-0.5 hover:bg-accent ${hasCustomParams ? "text-primary" : "text-muted-foreground"}`}
                            >
                              {expanded ? <ChevronDown className="h-3 w-3 inline" /> : <Settings2 className="h-3 w-3 inline" />}
                            </button>
                          )}
                          {/* Cost hint */}
                          {currentEp && a && (
                            <span className="text-muted-foreground ml-auto">{costHintForModel(provPrefix, a.model)}</span>
                          )}
                        </div>
                        {/* Advanced params drawer (inline) */}
                        {expanded && a && currentEp && (
                          <div className="ml-44 mt-1 mb-2 grid grid-cols-[7rem_8rem] gap-1.5 text-xs items-center bg-muted/20 rounded p-2 w-fit">
                            <label className="text-muted-foreground">temperature</label>
                            <input
                              type="number" step="0.1" min="0" max="2"
                              className="rounded border border-border bg-background px-1.5 py-0.5"
                              defaultValue={a.temperature ?? ""}
                              placeholder="(default)"
                              onBlur={(e) => {
                                const v = e.target.value === "" ? null : Number(e.target.value);
                                handleAssignmentParams(consumer, a.endpoint_id, a.model, {
                                  temperature: v, max_tokens: a.max_tokens, response_format: a.response_format, fallback_endpoint_id: a.fallback_endpoint_id,
                                });
                              }}
                            />
                            <label className="text-muted-foreground">max_tokens</label>
                            <input
                              type="number" min="1"
                              className="rounded border border-border bg-background px-1.5 py-0.5"
                              defaultValue={a.max_tokens ?? ""}
                              placeholder="(default)"
                              onBlur={(e) => {
                                const v = e.target.value === "" ? null : Number(e.target.value);
                                handleAssignmentParams(consumer, a.endpoint_id, a.model, {
                                  temperature: a.temperature, max_tokens: v, response_format: a.response_format, fallback_endpoint_id: a.fallback_endpoint_id,
                                });
                              }}
                            />
                            <label className="text-muted-foreground">response_format</label>
                            <select
                              className="rounded border border-border bg-background px-1.5 py-0.5"
                              value={a.response_format ?? ""}
                              onChange={(e) => {
                                const v = (e.target.value || null) as "text" | "json" | null;
                                handleAssignmentParams(consumer, a.endpoint_id, a.model, {
                                  temperature: a.temperature, max_tokens: a.max_tokens, response_format: v, fallback_endpoint_id: a.fallback_endpoint_id,
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
                                handleAssignmentParams(consumer, a.endpoint_id, a.model, {
                                  temperature: a.temperature, max_tokens: a.max_tokens, response_format: a.response_format, fallback_endpoint_id: v,
                                });
                              }}
                            >
                              <option value="">(none)</option>
                              {ep.endpoints.filter((e) => e.id !== a.endpoint_id).map((e) => (
                                <option key={e.id} value={e.id}>{e.name}</option>
                              ))}
                            </select>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ── Legacy-tab pointer ───────────────────────────────────────── */}
      <p className="text-[11px] text-muted-foreground">
        The legacy <span className="font-medium">Embedding</span> and <span className="font-medium">Agent Models</span> tabs still work and
        will be retired in a future release. AI Setup is the unified place to manage both.
      </p>

      <ToastViewport toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}
