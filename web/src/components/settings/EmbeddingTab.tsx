import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  CircleDot,
  Globe,
  HardDrive,
  HelpCircle,
  Languages,
  Loader2,
  PlugZap,
  Plus,
  X,
} from "lucide-react";
import { useEndpoints } from "@/hooks/useEndpoints";
import { useAssignments } from "@/hooks/useAssignments";
import { useReembedStatus } from "@/hooks/useReembedStatus";
import { useToast } from "@/hooks/useToast";
import { endpointSupportsEmbedding, presetSupportsEmbedding } from "@/lib/aiSetup";
import {
  estimateMigrationCost,
  formatCost,
  lookupModel,
} from "@/lib/knownEmbeddingModels";
import type { Assignment, Endpoint } from "@/lib/aiSetup";
import { AddEndpointPanel } from "./AddEndpointPanel";
import { ToastViewport } from "./ToastViewport";

// ── preset → embedding-provider key (for the knownEmbeddingModels lookup) ──
// ``knownEmbeddingModels`` keys are "<provider>/<model>" with provider names
// like ``jina_ai`` / ``openai`` / ``gemini`` / ``voyage`` / ``ollama``. The
// endpoint *preset* names mostly match — the one mismatch is ``google_ai``
// (the Gemini chat preset) → ``gemini`` in the embedding table.
function presetToEmbeddingProvider(preset: string): string {
  if (preset === "google_ai") return "gemini";
  return preset;
}

// Providers the legacy ``/api/settings/embedding`` + ``/migrate`` surface
// accepts (its ``EmbeddingProvider`` enum). An ``EMBEDDING_CAPABLE_PRESETS``
// endpoint whose provider isn't in here (``custom`` / ``litellm_proxy``) can
// still be *assigned* as the embedding endpoint, but can't drive the legacy
// re-embed job — that needs the backend re-home (B-i / PR6). Until then we
// block the "Start re-embed" action for those with a clear message rather
// than letting the legacy ``PUT`` 422 with no guidance.
const LEGACY_REEMBED_PROVIDERS = new Set<string>([
  "jina_ai",
  "openai",
  "cohere",
  "voyage",
  "gemini",
  "mistral",
  "ollama",
  "bedrock",
  "vertex_ai",
]);

interface DraftState {
  endpoint_id: string;
  model: string;
  dimensions: number | null;
  task: string | null;
}

function draftFromAssignment(a: Assignment | undefined): DraftState | null {
  if (!a) return null;
  return {
    endpoint_id: a.endpoint_id,
    model: a.model,
    dimensions: a.dimensions,
    task: a.task,
  };
}

function isDirty(a: Assignment | undefined, d: DraftState | null): boolean {
  if (!a || !d) return false;
  return (
    d.endpoint_id !== a.endpoint_id ||
    d.model !== a.model ||
    d.dimensions !== a.dimensions ||
    d.task !== a.task
  );
}

/**
 * ``/settings/embedding`` — replaces ``EmbeddingSettings`` visually but binds
 * the *config* to the ``embedding`` Assignment + its Endpoint via
 * ``useAssignments``/``useEndpoints`` (NOT ``useEmbeddingSettings``).
 *
 * ProviderTile-grid reuse note (plan Q7): ``ProviderTile`` is keyed on the
 * ``EmbeddingProvider`` enum + ``knownEmbeddingModels`` lookups, which doesn't
 * cleanly map onto "pick an *endpoint*". Per the plan's explicit fallback we
 * use a plain Endpoint ``<select>`` + Model ``<select>`` here and keep
 * ``ProviderTile``-style preset chips only inside ``AddEndpointPanel``. The
 * capability/cost pills below still come from ``knownEmbeddingModels``.
 *
 * Re-embed: the ``/migrate*`` endpoints + the legacy ``/api/settings/embedding``
 * config write are intentional (B-i) — see ``useReembedStatus`` and the plan.
 * The backend re-home is PR6, out of scope here.
 */
export function EmbeddingTab() {
  const ep = useEndpoints();
  const asn = useAssignments();
  const reembed = useReembedStatus();
  const { toasts, show: showToast, dismiss: dismissToast } = useToast();

  const [draft, setDraft] = useState<DraftState | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; latency_ms: number | null; error: string | null } | null>(null);
  const [showHelp, setShowHelp] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showAddEndpoint, setShowAddEndpoint] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [startingMigration, setStartingMigration] = useState(false);

  const assignment = useMemo(
    () => asn.assignments.find((a) => a.consumer === "embedding"),
    [asn.assignments],
  );
  const embeddingEndpoints = useMemo(
    () => ep.endpoints.filter((e) => endpointSupportsEmbedding(e)),
    [ep.endpoints],
  );
  const endpointById = useMemo(
    () => Object.fromEntries(ep.endpoints.map((e) => [e.id, e])) as Record<string, Endpoint>,
    [ep.endpoints],
  );

  // Initialise the draft once the assignment loads.
  useEffect(() => {
    if (assignment && !draft) setDraft(draftFromAssignment(assignment));
  }, [assignment, draft]);

  const chosenEndpoint: Endpoint | undefined = draft ? endpointById[draft.endpoint_id] : undefined;
  const provider = chosenEndpoint ? presetToEmbeddingProvider(chosenEndpoint.preset) : "";
  const spec = draft && provider ? lookupModel(provider, draft.model) : null;
  const dirty = isDirty(assignment, draft);
  const canLegacyReembed = !!provider && LEGACY_REEMBED_PROVIDERS.has(provider);

  function handleEndpoint(id: string) {
    const e = endpointById[id];
    if (!draft || !e) return;
    // Preserve the model if the new endpoint still offers it; else default to
    // the first model the new endpoint advertises.
    const nextModel = e.models.includes(draft.model) ? draft.model : (e.models[0] ?? draft.model);
    const nextSpec = lookupModel(presetToEmbeddingProvider(e.preset), nextModel);
    setDraft({
      ...draft,
      endpoint_id: id,
      model: nextModel,
      dimensions: nextSpec?.dim ?? draft.dimensions,
    });
    setTestResult(null);
  }

  function handleModel(model: string) {
    if (!draft) return;
    const nextSpec = lookupModel(provider, model);
    setDraft({ ...draft, model, dimensions: nextSpec?.dim ?? draft.dimensions });
    setTestResult(null);
  }

  async function handleTest() {
    if (!draft) return;
    setTesting(true);
    setTestResult(null);
    try {
      const r = await ep.test(draft.endpoint_id);
      setTestResult({ ok: r.ok, latency_ms: r.latency_ms, error: r.error });
    } catch (e: any) {
      setTestResult({ ok: false, latency_ms: null, error: e?.message ?? "test failed" });
    } finally {
      setTesting(false);
    }
  }

  async function handleSave() {
    if (!draft || !assignment) return;
    // Snapshot the target before the awaits — ``assignment`` updates async.
    const target: DraftState = { ...draft };
    const prevProvider = presetToEmbeddingProvider(endpointById[assignment.endpoint_id]?.preset ?? "");
    const providerOrModelOrDimChanged =
      provider !== prevProvider || target.model !== assignment.model || target.dimensions !== assignment.dimensions;
    setSaving(true);
    try {
      await asn.upsert("embedding", {
        endpoint_id: target.endpoint_id,
        model: target.model,
        dimensions: target.dimensions,
        task: target.task,
      });
      // B-i dual-write: if the provider/model/dimensions changed, also push the
      // legacy embedding config so the legacy ``/migrate`` path targets the new
      // model on the next re-embed. (Backend re-home = PR6, out of scope.)
      if (providerOrModelOrDimChanged && canLegacyReembed && target.dimensions != null) {
        try {
          await reembed.putLegacyConfig({ provider, model: target.model, dim: target.dimensions });
        } catch {
          // Legacy write failing isn't fatal — the new Assignment is the source
          // of truth; the re-embed banner / dim guard will surface the mismatch.
        }
      }
      await ep.refetch();
      await reembed.refetchStatus();
      setDraft(target);
      showToast("Embedding model saved");
    } catch (e: any) {
      showToast(e?.message ?? "Failed to save embedding model", "error");
    } finally {
      setSaving(false);
    }
  }

  async function handleStartReembed() {
    if (!draft || !provider || draft.dimensions == null) {
      setConfirmOpen(false);
      return;
    }
    if (!canLegacyReembed) {
      setConfirmOpen(false);
      showToast(
        `Re-embedding via a "${chosenEndpoint?.preset}" endpoint isn't supported yet — pick a direct provider (Jina, OpenAI, Gemini, Voyage, Ollama, …) to re-embed now.`,
        "error",
      );
      return;
    }
    setStartingMigration(true);
    try {
      // Push the legacy config to the chosen target, THEN spawn the job.
      await reembed.putLegacyConfig({ provider, model: draft.model, dim: draft.dimensions });
      await reembed.startMigration();
      await reembed.refetchStatus();
      setConfirmOpen(false);
      showToast("Re-embed started — watch progress above");
    } catch (e: any) {
      showToast(e?.message ?? "Failed to start re-embed", "error");
    } finally {
      setStartingMigration(false);
    }
  }

  const isLoading = ep.isLoading || asn.isLoading;
  const noEndpoints = !ep.isLoading && embeddingEndpoints.length === 0;
  const factCount = reembed.persisted?.count ?? 0;
  const migrationCost = useMemo(() => estimateMigrationCost(factCount, spec), [factCount, spec]);

  return (
    <div className="space-y-5">
      {/* ── Intro ───────────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm text-muted-foreground max-w-2xl">
          The embedding model turns text into vectors for semantic search. It's separate from the
          chat models because changing it requires re-embedding everything.
        </p>
        <button
          type="button"
          onClick={() => setShowHelp(true)}
          title="Picking an embedding provider"
          aria-label="Help: picking an embedding provider"
          className="shrink-0 p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted"
        >
          <HelpCircle className="h-4 w-4" />
        </button>
      </div>

      {(ep.error || asn.error) && (
        <div className="text-xs text-destructive bg-destructive/10 rounded-md px-3 py-2">{ep.error || asn.error}</div>
      )}

      {/* ── Re-embed progress block ─────────────────────────────────────── */}
      {reembed.status?.running && (() => {
        const processed = reembed.status.processed ?? 0;
        const total = reembed.status.total ?? 0;
        const pct = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : 0;
        const startedAt = reembed.status.started_at ? Date.parse(reembed.status.started_at) : null;
        const elapsedSec = startedAt ? (Date.now() - startedAt) / 1000 : 0;
        const etaMin =
          processed > 0 && total > processed
            ? Math.max(1, Math.round(((total - processed) / processed) * elapsedSec / 60))
            : null;
        return (
          <div className="px-4 py-3 rounded-lg bg-amber-500/10 border border-amber-500/30 flex items-start gap-3">
            <Loader2 className="w-4 h-4 animate-spin text-amber-600 dark:text-amber-400 mt-0.5" />
            <div className="text-xs flex-1 space-y-1.5">
              <div className="font-medium text-amber-700 dark:text-amber-300 flex items-center justify-between gap-2">
                <span>Re-embedding in progress · {reembed.status.stage ?? "starting"}</span>
                <span className="text-amber-600/80 dark:text-amber-400/80 font-mono">
                  {pct}%{etaMin != null ? ` · ETA ${etaMin}m` : ""}
                </span>
              </div>
              {total > 0 && (
                <div className="h-1.5 rounded-full bg-amber-500/20 overflow-hidden">
                  <div className="h-full bg-amber-500 transition-all duration-500" style={{ width: `${pct}%` }} />
                </div>
              )}
              <div className="text-amber-600/80 dark:text-amber-400/80">
                {processed.toLocaleString()} / {total.toLocaleString()} rows · Search degraded to keyword-only · Sync paused
              </div>
            </div>
          </div>
        );
      })()}

      {reembed.failedError && !reembed.status?.running && (
        <div className="px-4 py-3 rounded-lg bg-destructive/10 border border-destructive/30 flex items-start gap-3">
          <AlertTriangle className="w-4 h-4 text-destructive mt-0.5 shrink-0" />
          <div className="text-xs text-destructive space-y-1 flex-1">
            <div className="font-medium">Re-embed failed</div>
            <div className="font-mono text-[11px] break-all">{reembed.failedError}</div>
            <div className="text-destructive/80">
              Check the backend log for a full traceback. Click "Start re-embed" to retry — the migration is idempotent.
            </div>
          </div>
        </div>
      )}

      {reembed.migrationRequired && !reembed.status?.running && (
        <div className="px-4 py-3 rounded-lg bg-amber-500/10 border border-amber-500/30 flex items-start justify-between gap-3">
          <div className="flex items-start gap-3 flex-1">
            <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-400 mt-0.5 shrink-0" />
            <div className="text-xs text-amber-700 dark:text-amber-300 space-y-1">
              <div className="font-medium">Re-embed required</div>
              <div className="text-amber-600/90 dark:text-amber-400/90">
                {reembed.persisted ? (
                  <>
                    {(reembed.persisted.count ?? 0).toLocaleString()} stored vectors are still at{" "}
                    <code className="font-mono">
                      {reembed.persisted.provider}/{reembed.persisted.model}
                      {reembed.persisted.dim != null ? ` @ ${reembed.persisted.dim}d` : ""}
                    </code>
                    . Search falls back to keyword-only until the re-embed completes.
                  </>
                ) : (
                  <>The saved config differs from what's in storage. Search falls back to keyword-only until the re-embed completes.</>
                )}
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={() => setConfirmOpen(true)}
            disabled={!draft || !provider || draft.dimensions == null}
            className="text-xs px-3 py-1.5 rounded-md bg-amber-500 hover:bg-amber-600 disabled:opacity-50 text-white font-medium shrink-0"
          >
            Start re-embed
          </button>
        </div>
      )}

      {/* ── Endpoint + model picker ─────────────────────────────────────── */}
      {isLoading && !assignment ? (
        <div className="text-sm text-muted-foreground py-8 text-center">Loading embedding configuration…</div>
      ) : noEndpoints ? (
        <div className="rounded-xl border-2 border-dashed border-border bg-card p-6 text-center space-y-3">
          <div className="text-sm font-medium text-foreground">No embedding-capable endpoint yet</div>
          <p className="text-xs text-muted-foreground max-w-md mx-auto">
            Add a Jina, Voyage, OpenAI, Gemini, or Ollama endpoint, then pick it here as the embedding model.
          </p>
          {!showAddEndpoint && (
            <button
              type="button"
              onClick={() => setShowAddEndpoint(true)}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90"
            >
              <Plus className="w-4 h-4" />
              Add embedding endpoint
            </button>
          )}
          {showAddEndpoint && (
            <div className="text-left">
              <AddEndpointPanel
                presetFilter={presetSupportsEmbedding}
                onCancel={() => setShowAddEndpoint(false)}
                onCreate={async (req) => {
                  await ep.create(req);
                  setShowAddEndpoint(false);
                  showToast(`Endpoint '${req.name}' added`);
                }}
              />
            </div>
          )}
        </div>
      ) : draft ? (
        <div className="rounded-xl border border-border bg-card p-4 space-y-4">
          {/* Capability / cost pills */}
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {spec ? (
              <>
                <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border ${
                  spec.multilingual
                    ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30"
                    : "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/30"
                }`}>
                  <Languages className="w-3 h-3" />
                  {spec.multilingual ? "Multilingual" : "English-leaning"}
                </span>
                <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-muted text-muted-foreground border border-border">
                  {formatCost(spec)}
                </span>
                <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border ${
                  spec.local ? "bg-sky-500/10 text-sky-600 dark:text-sky-400 border-sky-500/30" : "bg-muted text-muted-foreground border-border"
                }`}>
                  {spec.local ? <HardDrive className="w-3 h-3" /> : <Globe className="w-3 h-3" />}
                  {spec.local ? "Local" : "Cloud"}
                </span>
              </>
            ) : (
              <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-muted text-muted-foreground border border-border">
                <CircleDot className="w-3 h-3" />
                Custom model — verify with Test Connection
              </span>
            )}
          </div>

          {/* Endpoint select */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Embedding endpoint</span>
            <div className="flex items-center gap-2">
              <select
                aria-label="embedding endpoint"
                value={draft.endpoint_id}
                onChange={(e) => handleEndpoint(e.target.value)}
                className="flex-1 text-sm bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
              >
                {!embeddingEndpoints.some((e) => e.id === draft.endpoint_id) && draft.endpoint_id && (
                  <option value={draft.endpoint_id}>{endpointById[draft.endpoint_id]?.name ?? draft.endpoint_id} (not embedding-capable)</option>
                )}
                {embeddingEndpoints.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name} ({e.preset})
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => setShowAddEndpoint((v) => !v)}
                className="inline-flex items-center gap-1 text-xs px-2.5 py-2 rounded-md border border-border hover:bg-muted shrink-0"
              >
                <Plus className="w-3 h-3" />
                Add
              </button>
            </div>
          </label>

          {showAddEndpoint && (
            <AddEndpointPanel
              presetFilter={presetSupportsEmbedding}
              onCancel={() => setShowAddEndpoint(false)}
              onCreate={async (req) => {
                await ep.create(req);
                setShowAddEndpoint(false);
                showToast(`Endpoint '${req.name}' added`);
              }}
            />
          )}

          {/* Model select */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Model</span>
            <select
              aria-label="embedding model"
              value={draft.model}
              onChange={(e) => handleModel(e.target.value)}
              className="text-sm bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
            >
              {chosenEndpoint && chosenEndpoint.models.length === 0 && (
                <option value={draft.model}>{draft.model || "(no models — run Discover on the Endpoints tab)"}</option>
              )}
              {chosenEndpoint && !chosenEndpoint.models.includes(draft.model) && draft.model && (
                <option value={draft.model}>{draft.model}</option>
              )}
              {chosenEndpoint?.models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>

          {/* Dimensions */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">
              Dimensions {spec && <span className="text-[10px] uppercase tracking-wide text-muted-foreground/70">· auto</span>}
            </span>
            <input
              type="number"
              aria-label="embedding dimensions"
              min={1}
              max={8192}
              value={draft.dimensions ?? ""}
              onChange={(e) => setDraft({ ...draft, dimensions: e.target.value === "" ? null : (Number(e.target.value) || 0) })}
              className="text-sm bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
            />
          </label>

          {/* Cost line — re-embed preview */}
          {factCount > 0 && (
            <div className="text-xs text-muted-foreground">
              Re-embedding {factCount.toLocaleString()} stored facts at this model ≈{" "}
              {spec?.local || (spec && spec.cost_per_m === 0) ? (
                <span className="font-mono">$0.00 (local)</span>
              ) : spec ? (
                <span className="font-mono">~${migrationCost.dollars.toFixed(2)}</span>
              ) : (
                <span>unknown — verify with Test Connection</span>
              )}
            </div>
          )}

          {/* Advanced — task */}
          <div>
            <button
              type="button"
              onClick={() => setShowAdvanced((v) => !v)}
              className="text-xs font-semibold text-muted-foreground uppercase tracking-wide flex items-center gap-1 hover:text-foreground"
            >
              <ChevronDown className={`w-3.5 h-3.5 transition-transform ${showAdvanced ? "" : "-rotate-90"}`} />
              Advanced — task hint
            </button>
            {showAdvanced && (
              <label className="flex flex-col gap-1.5 mt-3">
                <span className="text-xs font-medium text-muted-foreground">Task (optional — e.g. retrieval.passage)</span>
                <input
                  type="text"
                  aria-label="embedding task"
                  value={draft.task ?? ""}
                  onChange={(e) => setDraft({ ...draft, task: e.target.value || null })}
                  placeholder="leave blank for provider default"
                  className="text-sm bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
                />
              </label>
            )}
          </div>

          {/* Test result */}
          {testResult && (
            <div className={`rounded-md border px-3 py-2 text-xs ${
              testResult.ok
                ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/30"
                : "bg-destructive/10 text-destructive border-destructive/30"
            }`}>
              {testResult.ok ? (
                <>Test passed — endpoint responded in {testResult.latency_ms} ms.</>
              ) : (
                <>Test failed: {testResult.error ?? "unknown"}</>
              )}
            </div>
          )}

          {/* Actions */}
          <div className="flex items-center justify-end gap-2 pt-1">
            <button
              type="button"
              disabled={testing}
              onClick={handleTest}
              className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted disabled:opacity-50 inline-flex items-center gap-1.5"
            >
              {testing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <PlugZap className="w-3.5 h-3.5" />}
              Test Connection
            </button>
            {dirty && (
              <button
                type="button"
                onClick={() => setDraft(draftFromAssignment(assignment))}
                className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted"
              >
                Discard
              </button>
            )}
            <button
              type="button"
              disabled={!dirty || saving}
              onClick={handleSave}
              className="text-xs px-4 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 font-medium"
            >
              {saving ? "Saving…" : "Save Changes"}
            </button>
          </div>
        </div>
      ) : null}

      {showHelp && <HelpDrawer onClose={() => setShowHelp(false)} />}

      {confirmOpen && draft && (
        <MigrationConfirmModal
          factCount={factCount}
          targetProvider={provider || (chosenEndpoint?.preset ?? "")}
          targetModel={draft.model}
          submitting={startingMigration}
          onCancel={() => setConfirmOpen(false)}
          onConfirm={handleStartReembed}
        />
      )}

      <ToastViewport toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}

// ── salvaged: HelpDrawer (per-provider blurbs) ─────────────────────────────

function HelpDrawer({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-40 bg-background/60 backdrop-blur-sm flex justify-end" onClick={onClose}>
      <div
        className="w-full max-w-md h-full bg-card border-l border-border overflow-y-auto p-5 space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">Picking an embedding provider</h3>
          <button type="button" className="text-xs px-2 py-1 rounded-md hover:bg-muted" onClick={onClose}>
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="text-xs space-y-3 text-muted-foreground">
          <HelpSection title="Jina v4 (default, multilingual)">
            2048-dim, ~$0.18 / 1M tokens. Strong multilingual recall — recommended for channels with
            Chinese / Japanese / mixed-language content.
          </HelpSection>
          <HelpSection title="OpenAI text-embedding-3-large">
            3072-dim, ~$0.13 / 1M tokens. The most popular default; multilingual; supports ``dimensions=`` resizing.
          </HelpSection>
          <HelpSection title="Voyage 3-large">
            1024-dim, ~$0.18 / 1M tokens. Strong English; multilingual; honors a ``task=`` kwarg similar to Jina.
          </HelpSection>
          <HelpSection title="Gemini gemini-embedding-001">
            3072-dim default (Matryoshka-truncatable), ~$0.025 / 1M tokens. Reuses your Google AI key; multilingual.
          </HelpSection>
          <HelpSection title="Ollama (local)">
            ``nomic-embed-text`` 768d / ``mxbai-embed-large`` 1024d — free, local. Quality varies by model and is
            generally English-leaning. Best for self-hosted isolation; not recommended for multilingual channels.
          </HelpSection>
          <p>Switching providers on a populated install requires a one-time re-embed — kick it off from the banner above.</p>
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

// ── salvaged: MigrationConfirmModal ────────────────────────────────────────

function MigrationConfirmModal({
  factCount,
  targetProvider,
  targetModel,
  submitting,
  onCancel,
  onConfirm,
}: {
  factCount: number;
  targetProvider: string;
  targetModel: string;
  submitting: boolean;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
}) {
  const spec = useMemo(() => lookupModel(targetProvider, targetModel), [targetProvider, targetModel]);
  const cost = useMemo(() => estimateMigrationCost(factCount, spec), [factCount, spec]);
  return (
    <div className="fixed inset-0 z-50 bg-background/60 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="w-full max-w-md bg-card border border-border rounded-xl shadow-xl">
        <div className="px-5 py-4 border-b border-border flex items-center justify-between">
          <h3 className="text-sm font-semibold flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-500" />
            Re-embed required
          </h3>
          <button type="button" onClick={onCancel} className="text-muted-foreground hover:text-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="px-5 py-4 space-y-3 text-sm">
          <p>
            Re-embedding switches every stored fact + entity-name vector to{" "}
            <span className="font-mono">{targetProvider}/{targetModel}</span>. Search degrades to keyword-only
            (BM25) for a few minutes while the job runs; sync is paused during the window.
          </p>
          <div className="rounded-md border border-border bg-muted/30 px-3 py-2 text-xs space-y-1">
            <div>Facts to re-embed: <span className="font-mono">{factCount.toLocaleString()}</span></div>
            <div>
              Estimated cost:{" "}
              {spec?.local || (spec && spec.cost_per_m === 0) ? (
                <span className="font-mono">$0.00 (local)</span>
              ) : spec ? (
                <>
                  <span className="font-mono">~${cost.dollars.toFixed(2)}</span>{" "}
                  <span className="text-muted-foreground">({cost.tokens.toLocaleString()} tokens × ${spec.cost_per_m.toFixed(2)} / 1M)</span>
                </>
              ) : (
                <span className="text-muted-foreground">unknown — verify after Test Connection</span>
              )}
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            The job runs in the background; progress streams into the ingestion activity feed and the banner above.
            Resumable on failure.
          </p>
        </div>
        <div className="px-5 py-3 border-t border-border flex justify-end gap-2">
          <button type="button" className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted" onClick={onCancel} disabled={submitting}>
            Cancel
          </button>
          <button
            type="button"
            className="text-xs px-4 py-1.5 rounded-md bg-amber-600 text-white hover:bg-amber-600/90 disabled:opacity-50 font-medium inline-flex items-center gap-1.5"
            disabled={submitting}
            onClick={() => onConfirm()}
          >
            {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            Start re-embed
          </button>
        </div>
      </div>
    </div>
  );
}
