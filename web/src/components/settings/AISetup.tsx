import { useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  Eye,
  Hammer,
  KeyRound,
  Loader2,
  Mic,
  Plus,
  PlugZap,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { useEndpoints } from "@/hooks/useEndpoints";
import { useAssignments } from "@/hooks/useAssignments";
import {
  ENDPOINT_PRESETS,
  PRESET_LABELS,
  getEndpointPreset,
  type CreateEndpointRequest,
  type Endpoint,
} from "@/lib/aiSetup";
import { costHintForModel, isCompatible } from "@/lib/knownModels";

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

  const [showAddEndpoint, setShowAddEndpoint] = useState(false);
  const [presetBanner, setPresetBanner] = useState<{ preset: string; preserved: string[]; changed: number } | null>(null);
  const [busyEndpointId, setBusyEndpointId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; latency_ms: number | null; error: string | null }>>({});

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
    } catch {
      // useAssignments stores the error (e.g. preset_requirements_not_met);
      // the error banner below surfaces it.
    }
  }

  // ── endpoint actions ──────────────────────────────────────────────────
  async function handleTest(id: string) {
    setBusyEndpointId(id);
    try {
      const r = await ep.test(id);
      setTestResults((prev) => ({ ...prev, [id]: r }));
    } finally {
      setBusyEndpointId(null);
      await ep.refetch();
    }
  }

  async function handleDiscover(id: string) {
    setBusyEndpointId(id);
    try {
      const r = await ep.discover(id);
      if (r.ok && r.models.length > 0) {
        await ep.update(id, { models: r.models });
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
    } catch (e: any) {
      const detail = e?.body?.detail ?? e?.detail;
      if (detail?.error === "endpoint_in_use_as_primary_or_fallback") {
        alert(`In use by: ${(detail.consumers ?? []).join(", ")}. Reassign those first.`);
      }
    } finally {
      setBusyEndpointId(null);
    }
  }

  // ── assignment change ─────────────────────────────────────────────────
  async function handleAssignmentChange(consumer: string, endpointId: string, model: string) {
    try {
      await asn.upsert(consumer, { endpoint_id: endpointId, model });
    } catch {
      // hook stores the error (incompatible_assignment 422 etc.)
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
          <AddEndpointForm
            onCancel={() => setShowAddEndpoint(false)}
            onCreate={async (req) => {
              await ep.create(req);
              setShowAddEndpoint(false);
            }}
          />
        )}

        {ep.endpoints.length === 0 && !showAddEndpoint && (
          <p className="text-xs text-muted-foreground py-2">
            No endpoints configured yet. Add one above, or seed via the Quick Start presets.
          </p>
        )}

        <div className="space-y-2 mt-2">
          {ep.endpoints.map((e) => {
            const usedBy = asn.assignments.filter((a) => a.endpoint_id === e.id || a.fallback_endpoint_id === e.id);
            const tr = testResults[e.id];
            const status =
              e.has_credential === false && e.auth_type !== "none"
                ? { Icon: CircleDot, cls: "text-muted-foreground", label: "no key" }
                : tr
                  ? tr.ok
                    ? { Icon: CheckCircle2, cls: "text-green-600", label: `connected · ${tr.latency_ms}ms` }
                    : { Icon: AlertTriangle, cls: "text-destructive", label: tr.error ?? "failed" }
                  : e.last_test_ok === true
                    ? { Icon: CheckCircle2, cls: "text-green-600", label: "tested ok" }
                    : e.last_test_ok === false
                      ? { Icon: AlertTriangle, cls: "text-destructive", label: e.last_test_error ?? "last test failed" }
                      : { Icon: CircleDot, cls: "text-amber-600", label: "untested" };
            const busy = busyEndpointId === e.id;
            return (
              <div key={e.id} className="rounded-md border border-border px-3 py-2 text-xs">
                <div className="flex items-center gap-2">
                  <status.Icon className={`h-3.5 w-3.5 shrink-0 ${status.cls}`} />
                  <span className="font-medium">{e.name}</span>
                  <span className="text-muted-foreground">{e.preset}</span>
                  {e.has_credential && <span className="text-muted-foreground font-mono">{e.credential_masked}</span>}
                  <span className="text-muted-foreground ml-auto">{status.label}</span>
                </div>
                <div className="flex items-center gap-3 mt-1 text-muted-foreground">
                  <span className="truncate">{e.base_url || "(no base url)"}</span>
                  <span>· {e.models.length} models</span>
                  <span>· {usedBy.length} jobs</span>
                  <span>· {e.rpm} RPM</span>
                  <div className="ml-auto flex items-center gap-1">
                    <button onClick={() => handleTest(e.id)} disabled={busy} className="rounded px-1.5 py-0.5 hover:bg-accent disabled:opacity-50 flex items-center gap-1">
                      {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <PlugZap className="h-3 w-3" />} Test
                    </button>
                    <button onClick={() => handleDiscover(e.id)} disabled={busy} className="rounded px-1.5 py-0.5 hover:bg-accent disabled:opacity-50 flex items-center gap-1">
                      <RefreshCw className="h-3 w-3" /> Discover
                    </button>
                    <button onClick={() => handleDeleteEndpoint(e.id)} disabled={busy} className="rounded px-1.5 py-0.5 hover:bg-accent disabled:opacity-50 text-destructive">
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                </div>
              </div>
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
                    return (
                      <div key={consumer} className="flex items-center gap-2 text-xs py-0.5">
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
                        {/* Cost hint */}
                        {currentEp && a && (
                          <span className="text-muted-foreground ml-auto">{costHintForModel(provPrefix, a.model)}</span>
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
    </div>
  );
}

// ── Add Endpoint inline form ────────────────────────────────────────────────

function AddEndpointForm({
  onCancel,
  onCreate,
}: {
  onCancel: () => void;
  onCreate: (req: CreateEndpointRequest) => Promise<void>;
}) {
  const [presetKey, setPresetKey] = useState("openai");
  const preset = getEndpointPreset(presetKey);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState(preset?.base_url ?? "");
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<string[]>(preset?.default_models ?? []);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function onPresetChange(key: string) {
    setPresetKey(key);
    const p = getEndpointPreset(key);
    setBaseUrl(p?.base_url ?? "");
    setModels(p?.default_models ?? []);
    if (!name) setName(p?.label ?? key);
  }

  async function submit() {
    setSaving(true);
    setErr(null);
    try {
      const authType = preset?.auth_type ?? "api_key";
      await onCreate({
        name: name || presetKey,
        preset: presetKey,
        base_url: baseUrl,
        auth_type: authType,
        api_key: authType === "none" ? undefined : apiKey || undefined,
        models,
      });
    } catch (e: any) {
      const detail = e?.body?.detail ?? e?.detail;
      setErr(detail?.error ? `${detail.error}` : (e?.message ?? "failed to create endpoint"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rounded-md border border-border bg-muted/30 p-3 mb-3 space-y-2 text-xs">
      <div className="flex flex-wrap gap-1.5">
        {ENDPOINT_PRESETS.map((p) => (
          <button
            key={p.key}
            onClick={() => onPresetChange(p.key)}
            className={`rounded px-2 py-0.5 border ${p.key === presetKey ? "border-primary bg-primary/10" : "border-border hover:bg-accent"}`}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-[6rem_1fr] gap-2 items-center mt-2">
        <label>Name</label>
        <input className="rounded border border-border bg-background px-2 py-1" value={name} onChange={(e) => setName(e.target.value)} placeholder={preset?.label ?? presetKey} />
        <label>Base URL</label>
        <input className="rounded border border-border bg-background px-2 py-1 font-mono" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.example.com/v1" />
        {preset?.auth_type !== "none" && (
          <>
            <label>API key</label>
            <input className="rounded border border-border bg-background px-2 py-1 font-mono" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-..." />
          </>
        )}
        <label>Models</label>
        <input
          className="rounded border border-border bg-background px-2 py-1 font-mono"
          value={models.join(", ")}
          onChange={(e) => setModels(e.target.value.split(",").map((m) => m.trim()).filter(Boolean))}
          placeholder="comma-separated model names"
        />
      </div>
      {preset?.docs_url && (
        <a href={preset.docs_url} target="_blank" rel="noreferrer" className="text-muted-foreground underline">
          Get an API key →
        </a>
      )}
      {err && <div className="text-destructive">{err}</div>}
      <div className="flex gap-2">
        <button onClick={submit} disabled={saving} className="rounded bg-primary text-primary-foreground px-3 py-1 disabled:opacity-50 flex items-center gap-1">
          {saving && <Loader2 className="h-3 w-3 animate-spin" />} Save
        </button>
        <button onClick={onCancel} className="rounded border border-border px-3 py-1 hover:bg-accent">Cancel</button>
      </div>
    </div>
  );
}
