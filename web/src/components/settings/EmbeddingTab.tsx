import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  Gauge,
  Globe,
  HardDrive,
  HelpCircle,
  Languages,
  Loader2,
  Lock,
  PlugZap,
  Plus,
  X,
} from "lucide-react";
import { useEndpoints } from "@/hooks/useEndpoints";
import { useAssignments } from "@/hooks/useAssignments";
import { useReembedStatus } from "@/hooks/useReembedStatus";
import { useToast } from "@/hooks/useToast";
import {
  endpointSupportsEmbedding,
  getEndpointPreset,
  presetSupportsEmbedding,
} from "@/lib/aiSetup";
import {
  estimateMigrationCost,
  formatCost,
  formatDollars,
  lookupModel,
  modelsForProvider,
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

// Sentinel value for the "Other (custom model)…" <option>. A model name will
// never literally be this string.
const CUSTOM_MODEL_OPTION = "__custom__";

/**
 * PR-γ: True when the endpoint's classifier has run AND it produced zero
 * embedding-classified models. Drives the "(no embedding models — run
 * Discover)" hint on the endpoint picker. Pre-α endpoints (no ``model_kinds``)
 * stay quiet — we don't know either way.
 */
function endpointHasNoEmbeddingModels(e: Endpoint): boolean {
  const kinds = e.model_kinds;
  if (!kinds || Object.keys(kinds).length === 0) return false;
  return !Object.values(kinds).some((k) => k === "embedding");
}

/**
 * Clean display label for an endpoint <option> — mirrors
 * ``EndpointCard.resolveDisplayName``: an endpoint hydrated from the env shim
 * gets a noisy ``"<preset> (from GOOGLE_API_KEY)"`` name + the
 * ``migrated-from-env`` tag, so for those we show the preset's friendly label
 * + a discreet "(auto-detected)" hint instead of the raw name; otherwise the
 * operator-set name as-is. Never a ``(preset)`` suffix.
 */
function endpointLabel(e: Endpoint): string {
  const autoName = e.name.startsWith(`${e.preset} (from `) && e.name.endsWith(")");
  const taggedFromEnv = e.tags.includes("migrated-from-env");
  if (autoName || taggedFromEnv) {
    const friendly = getEndpointPreset(e.preset)?.label ?? e.preset;
    return `${friendly} (auto-detected)`;
  }
  return e.name;
}

interface DraftState {
  endpoint_id: string;
  /** The known/selected model name. When ``customModel`` is non-empty this is
   *  ignored in favour of it (the <select> shows the "Other…" option). */
  model: string;
  /** Non-empty ⇒ a free-text custom model name (the "Other…" escape hatch). */
  customModel: string;
  task: string | null;
}

/** The effective model name a draft resolves to (custom wins). */
function effectiveModel(d: DraftState): string {
  return d.customModel.trim() || d.model;
}

function draftFromAssignment(
  a: Assignment | undefined,
  endpointById: Record<string, Endpoint>,
): DraftState | null {
  if (!a) return null;
  const e = endpointById[a.endpoint_id];
  const provider = e ? presetToEmbeddingProvider(e.preset) : "";
  const known = provider ? modelsForProvider(provider) : [];
  const isKnown = a.model !== "" && known.includes(a.model);
  return {
    endpoint_id: a.endpoint_id,
    model: isKnown ? a.model : "",
    customModel: a.model !== "" && !isKnown ? a.model : "",
    task: a.task,
  };
}

function isDirty(a: Assignment | undefined, d: DraftState | null): boolean {
  if (!a || !d) return false;
  return d.endpoint_id !== a.endpoint_id || effectiveModel(d) !== a.model || (d.task ?? "") !== (a.task ?? "");
}

/**
 * ``/settings/embedding`` — binds the embedding *config* to the ``embedding``
 * Assignment + its Endpoint (``useAssignments`` / ``useEndpoints``) and the
 * re-embed machinery to ``useReembedStatus``.
 *
 * Layout (top → bottom):
 *   1. Intro line + the "?" provider-help drawer.
 *   2. Re-embed status region — prominent, near the top: running progress
 *      banner / "re-embed required" banner / "last re-embed failed" banner /
 *      a quiet "up to date" pill. While a job is running the config form is
 *      locked (the operator shouldn't change the target mid-job).
 *   3. The embedding switch — endpoint picker (clean labels, "+ Add embedding
 *      endpoint"), Model picker sourced from ``KNOWN_EMBEDDING_MODELS`` for the
 *      chosen endpoint's *provider* (chat models are never offered) with an
 *      "Other (custom model)…" escape hatch, a read-only dim/cost line (no
 *      Dimensions input — the dim is a property of the model), an Advanced
 *      ``task`` hint, Test Connection, and explicit Save / Discard.
 *   4. Toasts.
 *
 * On Save: ``asn.upsert("embedding", { endpoint_id, model, dimensions, task })``
 * where ``dimensions`` is the known model's ``dim`` (from ``lookupModel``) or
 * ``null`` for a custom/unknown model — the backend probes the model and the
 * dim-guard records the real dimension at re-embed time. Then, if a re-embed is
 * warranted (there's persisted data AND the new model's dim/provider differs,
 * or it can't be determined), the ``MigrationConfirmModal`` opens; on confirm
 * ``reembed.startMigration()`` runs (it hits the non-deprecated
 * ``/api/settings/embedding-migration/spawn`` which reads the just-saved
 * Assignment server-side — so the order is upsert first, then startMigration).
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
  // The migration-confirm modal: ``null`` = closed, otherwise the context the
  // modal previews (the persisted snapshot it's migrating *from* + whether the
  // config was already saved or still pending a save-then-spawn).
  const [confirm, setConfirm] = useState<{ afterSave: boolean } | null>(null);
  const [startingMigration, setStartingMigration] = useState(false);

  const assignment = useMemo(
    () => asn.assignments.find((a) => a.consumer === "embedding"),
    [asn.assignments],
  );
  const endpointById = useMemo(
    () => Object.fromEntries(ep.endpoints.map((e) => [e.id, e])) as Record<string, Endpoint>,
    [ep.endpoints],
  );
  const embeddingEndpoints = useMemo(
    () => ep.endpoints.filter((e) => endpointSupportsEmbedding(e)),
    [ep.endpoints],
  );

  // Initialise the draft once both the assignment + endpoints have loaded
  // (we need the endpoint map to tell a known model from a custom one).
  useEffect(() => {
    if (assignment && !draft && ep.endpoints.length > 0) {
      setDraft(draftFromAssignment(assignment, endpointById));
    }
  }, [assignment, draft, ep.endpoints.length, endpointById]);

  const chosenEndpoint: Endpoint | undefined = draft ? endpointById[draft.endpoint_id] : undefined;
  // ``desiredProvider``/``desiredModel`` — what the operator is *configuring*
  // (the chosen endpoint's embedding-provider key + the effective model name,
  // known or custom). Distinct from ``persisted`` below, which is what's
  // actually running in Weaviate right now.
  const provider = chosenEndpoint ? presetToEmbeddingProvider(chosenEndpoint.preset) : "";
  const desiredProvider = provider;
  const knownModels = provider ? modelsForProvider(provider) : [];
  // PR-γ: operator-promoted embedding entries from ``endpoint.models[]`` that
  // aren't in ``KNOWN_EMBEDDING_MODELS``. We surface them in the Model picker
  // *only* when the classifier tagged them ``"embedding"`` — that's the case
  // where the operator hit "+ Promote" on an advanced model that turned out
  // to be embedding-shaped. Pre-α endpoints (empty ``model_kinds``) keep the
  // known-only list to avoid offering chat models by accident.
  const promotedEmbeddingModels: string[] = useMemo(() => {
    if (!chosenEndpoint) return [];
    const kinds = chosenEndpoint.model_kinds;
    if (!kinds || Object.keys(kinds).length === 0) return [];
    return chosenEndpoint.models.filter(
      (m) => kinds[m] === "embedding" && !knownModels.includes(m),
    );
  }, [chosenEndpoint, knownModels]);
  const effModel = draft ? effectiveModel(draft) : "";
  const desiredModel = effModel;
  const usingCustom = !!draft && (draft.customModel.trim().length > 0 || knownModels.length === 0);
  const spec = provider && effModel && !usingCustom ? lookupModel(provider, effModel) : null;
  const knownDim = spec?.dim ?? null;
  const dirty = isDirty(assignment, draft);

  // ── Re-embed state derived from the hook ──────────────────────────────────
  const running = !!reembed.status?.running;
  const persisted = reembed.persisted;
  const factCount = persisted?.count ?? 0;
  // True when there's data in storage AND it's running a *different*
  // provider/model than what the operator has currently configured. The
  // backend's ``migration_required`` only compares *dimensions* — and an
  // unknown model has an unknown dim, so a botched hydration (chat model in
  // the embedding Assignment) slips past it. This client-side name check
  // catches that and is what flips the banner from green to amber.
  const configMismatch =
    !!persisted &&
    (persisted.count ?? 0) > 0 &&
    !!desiredProvider &&
    !!desiredModel &&
    (persisted.provider !== desiredProvider || persisted.model !== desiredModel);
  // The amber "re-embed required" banner shows when the backend says so OR
  // when the running model differs from the configured one.
  const reembedRequired = reembed.migrationRequired || configMismatch;
  // Re-embed support for the *currently chosen* endpoint. ``reembedSupported``
  // reflects the *saved* Assignment's endpoint — if the draft's endpoint
  // differs we still gate the action and surface the backend reason once the
  // Assignment has been saved + the state re-read. For a custom endpoint
  // (preset with no known embedding models, e.g. ``custom``/``litellm_proxy``)
  // the backend may also refuse — we lean on its ``reembedSupportReason``.
  const canReembed = reembed.reembedSupported;
  const migrationCost = useMemo(() => estimateMigrationCost(factCount, spec), [factCount, spec]);

  // Form is read-only while a job runs OR while loading.
  const formLocked = running;

  function handleEndpoint(id: string) {
    const e = endpointById[id];
    if (!draft || !e) return;
    const nextProvider = presetToEmbeddingProvider(e.preset);
    const nextKnown = modelsForProvider(nextProvider);
    // Keep the model if the new provider still knows it; else default to its
    // first known embedding model (or the custom escape hatch when it has none).
    let model = "";
    let customModel = "";
    const cur = effectiveModel(draft);
    if (cur && nextKnown.includes(cur)) model = cur;
    else if (nextKnown.length > 0) model = nextKnown[0];
    else customModel = cur; // no known models for this provider — preserve as custom
    setDraft({ ...draft, endpoint_id: id, model, customModel });
    setTestResult(null);
  }

  function handleModelSelect(value: string) {
    if (!draft) return;
    if (value === CUSTOM_MODEL_OPTION) {
      // Switch to the free-text escape hatch. Seed it with the current model
      // name so the user can tweak rather than retype.
      setDraft({ ...draft, model: "", customModel: draft.customModel || draft.model || "" });
    } else {
      setDraft({ ...draft, model: value, customModel: "" });
    }
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

  /**
   * Decide whether saving this draft warrants a re-embed: yes when there's
   * persisted data AND (the new known dim differs from the persisted dim, OR
   * the new provider differs from the persisted provider, OR we genuinely
   * can't tell — a custom/unknown model). Conservative on purpose.
   */
  function needsReembedAfterSave(): boolean {
    if (!persisted || (persisted.count ?? 0) <= 0) return false;
    if (provider && persisted.provider && provider !== persisted.provider) return true;
    if (knownDim != null && persisted.dim != null) {
      // Same provider + a known dim that matches what's in storage → the new
      // vectors land in the same space, no re-embed needed.
      return knownDim !== persisted.dim;
    }
    // Can't determine the new dim (custom/unknown model) but there *is* data —
    // be conservative and offer the re-embed.
    return true;
  }

  async function handleSave() {
    if (!draft || !assignment) return;
    const target: DraftState = { ...draft };
    const targetModel = effectiveModel(target);
    if (!targetModel) {
      showToast("Pick (or type) an embedding model first", "error");
      return;
    }
    setSaving(true);
    try {
      await asn.upsert("embedding", {
        endpoint_id: target.endpoint_id,
        model: targetModel,
        // Known model → its dim; custom/unknown → null (backend probes + the
        // dim-guard records the real dimension at re-embed time).
        dimensions: knownDim,
        task: target.task,
      });
      await ep.refetch();
      await reembed.refetchStatus();
      setDraft(target);
      if (needsReembedAfterSave()) {
        showToast("Embedding model saved — re-embed needed");
        setConfirm({ afterSave: true });
      } else {
        showToast("Embedding model saved");
      }
    } catch (e: any) {
      showToast(e?.message ?? "Failed to save embedding model", "error");
    } finally {
      setSaving(false);
    }
  }

  async function handleStartReembed() {
    if (!canReembed) {
      setConfirm(null);
      showToast(
        reembed.reembedSupportReason ??
          `Re-embedding via a "${chosenEndpoint?.preset}" endpoint isn't supported yet — pick a direct provider (Jina, OpenAI, Gemini, Voyage, Ollama, …) to re-embed now.`,
        "error",
      );
      return;
    }
    setStartingMigration(true);
    try {
      // ``/spawn`` reads the just-saved ``embedding`` Assignment server-side,
      // dual-writes the legacy embedding_settings doc, then spawns the job.
      await reembed.startMigration();
      await reembed.refetchStatus();
      setConfirm(null);
      showToast("Re-embed started — watch progress above");
    } catch (e: any) {
      showToast(e?.message ?? "Failed to start re-embed", "error");
    } finally {
      setStartingMigration(false);
    }
  }

  const isLoading = ep.isLoading || asn.isLoading;
  const noEndpoints = !ep.isLoading && embeddingEndpoints.length === 0;

  return (
    <div className="space-y-4">
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

      {/* ── Re-embed status region — prominent, near the top ─────────────── */}
      {running && (() => {
        const processed = reembed.status?.processed ?? 0;
        const total = reembed.status?.total ?? 0;
        const pct = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : 0;
        const startedAt = reembed.status?.started_at ? Date.parse(reembed.status.started_at) : null;
        const elapsedSec = startedAt ? (Date.now() - startedAt) / 1000 : 0;
        const etaMin =
          processed > 0 && total > processed
            ? Math.max(1, Math.round(((total - processed) / processed) * elapsedSec / 60))
            : null;
        return (
          <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3.5">
            <div className="flex items-start gap-3">
              <Loader2 className="h-4 w-4 animate-spin text-amber-600 dark:text-amber-400 mt-0.5 shrink-0" />
              <div className="flex-1 min-w-0 space-y-2">
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <span className="text-sm font-semibold text-amber-700 dark:text-amber-300">
                    Re-embedding in progress
                  </span>
                  <span className="font-mono text-xs text-amber-600/90 dark:text-amber-400/90">
                    {pct}%{etaMin != null ? ` · ~${etaMin}m left` : ""}
                  </span>
                </div>
                <div className="h-1.5 rounded-full bg-amber-500/20 overflow-hidden">
                  <div className="h-full bg-amber-500 transition-all duration-700 ease-out" style={{ width: `${pct}%` }} />
                </div>
                <div className="text-xs text-amber-700/90 dark:text-amber-300/90">
                  {processed.toLocaleString()} / {total.toLocaleString()} facts · {reembed.status?.stage ?? "starting"} ·
                  search degraded to keyword-only · sync paused
                </div>
                <Link
                  to="/activity"
                  className="inline-flex items-center gap-1 text-[11px] font-medium text-amber-700 dark:text-amber-300 hover:underline"
                >
                  <Gauge className="w-3 h-3" /> View in Activity
                </Link>
              </div>
            </div>
          </div>
        );
      })()}

      {reembed.failedError && !running && (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3.5">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-start gap-3 flex-1 min-w-0">
              <AlertTriangle className="h-4 w-4 text-destructive mt-0.5 shrink-0" />
              <div className="text-xs text-destructive space-y-1 min-w-0">
                <div className="text-sm font-semibold">Last re-embed failed</div>
                <div className="font-mono text-[11px] break-all">{reembed.failedError}</div>
                <div className="text-destructive/80">
                  Search is still on the old model. The re-embed is idempotent — retrying is safe.
                </div>
              </div>
            </div>
            <button
              type="button"
              onClick={() => setConfirm({ afterSave: false })}
              disabled={!draft || !canReembed}
              title={!canReembed ? (reembed.reembedSupportReason ?? "Re-embed not supported for this endpoint") : undefined}
              className="text-xs px-3 py-1.5 rounded-md bg-destructive text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50 font-medium shrink-0"
            >
              Retry
            </button>
          </div>
        </div>
      )}

      {reembedRequired && !running && !reembed.failedError && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-start gap-3 flex-1 min-w-0">
              <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-400 mt-0.5 shrink-0" />
              <div className="text-xs text-amber-700 dark:text-amber-300 space-y-1 min-w-0">
                <div className="text-sm font-semibold">Re-embed required</div>
                <div className="text-amber-700/90 dark:text-amber-300/90">
                  {persisted && desiredProvider && desiredModel ? (
                    <>
                      You've configured{" "}
                      <code className="font-mono">{desiredProvider}/{desiredModel}</code>, but search is still
                      running on{" "}
                      <code className="font-mono">
                        {persisted.provider}/{persisted.model}
                        {persisted.dim != null ? ` @ ${persisted.dim}d` : ""}
                      </code>{" "}
                      ({(persisted.count ?? 0).toLocaleString()} facts).{" "}
                      {dirty ? "Save your change and re-embed to apply it." : "Re-embed to apply it."}
                    </>
                  ) : persisted ? (
                    <>
                      {(persisted.count ?? 0).toLocaleString()} facts are still on the old model (
                      <code className="font-mono">
                        {persisted.provider}/{persisted.model}
                        {persisted.dim != null ? ` @ ${persisted.dim}d` : ""}
                      </code>
                      ); search uses the old vectors until you re-embed.
                    </>
                  ) : (
                    <>The saved config differs from what's in storage; search uses the old vectors until you re-embed.</>
                  )}
                </div>
                {!canReembed && reembed.reembedSupportReason && (
                  <div className="text-amber-600/80 dark:text-amber-400/80">{reembed.reembedSupportReason}</div>
                )}
              </div>
            </div>
            <button
              type="button"
              onClick={() => setConfirm({ afterSave: false })}
              disabled={!draft || !canReembed}
              title={!canReembed ? (reembed.reembedSupportReason ?? "Re-embed not supported for this endpoint") : undefined}
              className="text-xs px-3 py-1.5 rounded-md bg-amber-500 hover:bg-amber-600 disabled:opacity-50 text-white font-medium shrink-0"
            >
              Start re-embed
            </button>
          </div>
        </div>
      )}

      {!running && !reembed.failedError && !reembedRequired && persisted && (
        <div className="inline-flex items-center gap-2 rounded-full bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border border-emerald-500/30 px-3 py-1.5 text-xs">
          <CheckCircle2 className="w-3.5 h-3.5" />
          <span>
            <span className="font-medium">Embeddings up to date</span> — {(persisted.count ?? 0).toLocaleString()} facts on{" "}
            <code className="font-mono">{persisted.provider}/{persisted.model}</code>
          </span>
        </div>
      )}

      {!running && !reembed.failedError && !reembedRequired && !persisted && (
        <div className="text-xs text-muted-foreground">No facts embedded yet — search activates once the first facts land.</div>
      )}

      {/* ── The embedding switch — config form ──────────────────────────── */}
      {isLoading && !draft ? (
        <div className="text-sm text-muted-foreground py-8 text-center">Loading embedding configuration…</div>
      ) : noEndpoints ? (
        <div className="rounded-xl border-2 border-dashed border-border bg-card p-6 text-center space-y-3">
          <div className="text-sm font-medium text-foreground">Add an embedding provider to start</div>
          <p className="text-xs text-muted-foreground max-w-md mx-auto">
            Atlas needs an embedding endpoint before semantic search works. Good defaults:{" "}
            <span className="font-medium text-foreground">Jina v4</span> (multilingual),{" "}
            <span className="font-medium text-foreground">OpenAI text-embedding-3-large</span>, or{" "}
            <span className="font-medium text-foreground">Voyage 3-large</span>. Ollama runs locally and is free.
          </p>
          <button
            type="button"
            onClick={() => setShowAddEndpoint(true)}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90"
          >
            <Plus className="w-4 h-4" />
            Add embedding endpoint
          </button>
        </div>
      ) : draft ? (
        <div
          className={`rounded-xl border border-border bg-card p-4 space-y-3.5 transition-opacity ${
            formLocked ? "opacity-60 pointer-events-none select-none" : ""
          }`}
          aria-disabled={formLocked || undefined}
        >
          {formLocked && (
            <div className="flex items-center gap-2 text-xs text-amber-700 dark:text-amber-300 bg-amber-500/10 rounded-md px-3 py-2">
              <Lock className="w-3.5 h-3.5 shrink-0" />
              Embedding config is locked while re-embedding — wait for the job above to finish.
            </div>
          )}

          {/* Endpoint / provider select */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Provider / endpoint</span>
            <div className="flex items-center gap-2">
              <select
                aria-label="embedding endpoint"
                value={draft.endpoint_id}
                onChange={(e) => handleEndpoint(e.target.value)}
                disabled={formLocked}
                className="flex-1 text-sm bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
              >
                {!embeddingEndpoints.some((e) => e.id === draft.endpoint_id) && draft.endpoint_id && (
                  <option value={draft.endpoint_id}>
                    {endpointById[draft.endpoint_id] ? endpointLabel(endpointById[draft.endpoint_id]) : draft.endpoint_id} (not embedding-capable)
                  </option>
                )}
                {embeddingEndpoints.map((e) => {
                  // PR-γ: when an endpoint's classifier has run AND produced
                  // zero embedding models, hint the operator to re-Discover.
                  // Pre-α endpoints with no ``model_kinds`` stay quiet.
                  const noEmb = endpointHasNoEmbeddingModels(e);
                  return (
                    <option key={e.id} value={e.id}>
                      {endpointLabel(e)}
                      {noEmb ? " (no embedding models — run Discover)" : ""}
                    </option>
                  );
                })}
              </select>
              <button
                type="button"
                onClick={() => setShowAddEndpoint(true)}
                disabled={formLocked}
                className="inline-flex items-center gap-1 text-xs px-2.5 py-2 rounded-md border border-border hover:bg-muted shrink-0 disabled:opacity-50"
              >
                <Plus className="w-3 h-3" />
                Add embedding endpoint
              </button>
            </div>
          </label>

          {/* Model select — embedding models for the chosen provider only,
              plus the "Other (custom model)…" escape hatch. */}
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Model</span>
            <select
              aria-label="embedding model"
              value={usingCustom ? CUSTOM_MODEL_OPTION : draft.model}
              onChange={(e) => handleModelSelect(e.target.value)}
              disabled={formLocked}
              className="text-sm bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
            >
              {/* When the saved model is unknown for this provider keep it as a
                  visible option (so the <select> isn't blank) — but in practice
                  ``usingCustom`` already routes those to the custom input. */}
              {knownModels.map((m) => {
                const s = lookupModel(provider, m);
                const label = s
                  ? `${m} — ${s.dim}-dim, ${s.multilingual ? "multilingual" : "English-leaning"}, ${formatCost(s)}`
                  : m;
                return (
                  <option key={m} value={m}>
                    {label}
                  </option>
                );
              })}
              {/* PR-γ: operator-promoted embedding models — entries that the
                  classifier tagged as ``"embedding"`` but that aren't in
                  ``KNOWN_EMBEDDING_MODELS``. Shown with a small "(promoted)"
                  hint so they're obviously not a curated default. */}
              {promotedEmbeddingModels.map((m) => (
                <option key={`__promoted__${m}`} value={m}>
                  {m} — promoted (dim verified at re-embed)
                </option>
              ))}
              <option value={CUSTOM_MODEL_OPTION}>Other (custom model)…</option>
            </select>
          </label>

          {/* "Currently in use" reference — what's actually running in Weaviate
              right now, distinct from what's being configured above. Always
              shown when there's persisted data so the operator can spot a
              config-vs-live mismatch immediately. */}
          {persisted && (
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground -mt-1.5">
              <CircleDot className="w-3 h-3 shrink-0" />
              <span>
                Currently in use:{" "}
                <code className="font-mono text-foreground/80">{persisted.provider}/{persisted.model}</code>
                {persisted.dim != null ? ` · ${persisted.dim}-dim` : ""}
                {(persisted.count ?? 0) > 0 ? ` · ${(persisted.count ?? 0).toLocaleString()} facts` : ""}
              </span>
              <span className="ml-0.5 px-1 py-px rounded text-[10px] font-medium uppercase tracking-wide bg-muted text-muted-foreground border border-border">
                live
              </span>
            </div>
          )}

          {/* "About to change" — the explicit before→after once the form is
              dirty and the configured model differs from what's running. */}
          {dirty && desiredProvider && desiredModel &&
            (!persisted || persisted.provider !== desiredProvider || persisted.model !== desiredModel) && (
            <div className="flex items-center gap-1.5 text-xs text-amber-700 dark:text-amber-400 -mt-1">
              <ArrowRight className="w-3 h-3 shrink-0" />
              <span>
                Changing to:{" "}
                <code className="font-mono">{desiredProvider}/{desiredModel}</code>
                {knownDim != null ? ` · ${knownDim}-dim` : " · dimension verified at re-embed"}
                {" — Save"}
                {(persisted?.count ?? 0) > 0 ? ` (re-embeds ${(persisted!.count ?? 0).toLocaleString()} facts)` : ""}.
              </span>
            </div>
          )}

          {/* Custom model free-text input — revealed by "Other (custom model)…" */}
          {usingCustom && (
            <label className="flex flex-col gap-1.5">
              <span className="text-xs font-medium text-muted-foreground">Custom model name</span>
              <input
                type="text"
                aria-label="custom embedding model"
                value={draft.customModel}
                onChange={(e) => setDraft({ ...draft, customModel: e.target.value, model: "" })}
                disabled={formLocked}
                placeholder={
                  provider === "ollama" ? "e.g. nomic-embed-text:v1.5" : "e.g. text-embedding-3-large or a proxy model id"
                }
                className="text-sm font-mono bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
              />
            </label>
          )}

          {/* Read-only model facts — a tidy row of small pills (dim ·
              multilingual · cost · cloud/local), then the re-embed-cost
              estimate as a separate muted line when there's data. No
              Dimensions input (the dim is a property of the model; the
              backend probes it and the dim-guard records the real dimension
              at re-embed time). */}
          {!usingCustom && spec ? (
            <div className="space-y-1.5">
              <div className="flex flex-wrap items-center gap-1.5 text-xs">
                <span className="inline-flex items-center px-2 py-0.5 rounded-md bg-muted text-muted-foreground border border-border font-mono">
                  {spec.dim}-dim
                </span>
                <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border ${
                  spec.multilingual
                    ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30"
                    : "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/30"
                }`}>
                  <Languages className="w-3 h-3" />
                  {spec.multilingual ? "multilingual" : "English-leaning"}
                </span>
                <span className="inline-flex items-center px-2 py-0.5 rounded-md bg-muted text-muted-foreground border border-border">
                  {formatCost(spec)}
                </span>
                <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border ${
                  spec.local ? "bg-sky-500/10 text-sky-600 dark:text-sky-400 border-sky-500/30" : "bg-muted text-muted-foreground border-border"
                }`}>
                  {spec.local ? <HardDrive className="w-3 h-3" /> : <Globe className="w-3 h-3" />}
                  {spec.local ? "local" : "cloud"}
                </span>
              </div>
              {factCount > 0 && (
                <div className="text-xs text-muted-foreground">
                  Re-embedding {factCount.toLocaleString()} facts ≈{" "}
                  {spec.local || spec.cost_per_m === 0 ? (
                    <span className="font-mono">free (local)</span>
                  ) : (
                    <span className="font-mono">{formatDollars(migrationCost.dollars)}</span>
                  )}
                </div>
              )}
            </div>
          ) : (
            <div className="flex items-start gap-2 text-xs text-muted-foreground">
              <CircleDot className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <div className="space-y-0.5">
                <div>Dimension verified at re-embed time — the backend probes the model on first use.</div>
                {reembed.reembedSupportReason && (
                  <div className="text-amber-600/80 dark:text-amber-400/80">
                    Heads up: {reembed.reembedSupportReason}
                  </div>
                )}
                {!reembed.reembedSupportReason && (
                  <div>Re-embed support depends on the provider — confirm with Test Connection first.</div>
                )}
              </div>
            </div>
          )}

          {/* Advanced — task hint (collapsed) */}
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
                <span className="text-xs font-medium text-muted-foreground">
                  Task (optional — e.g. <code className="font-mono">retrieval.passage</code>)
                </span>
                <input
                  type="text"
                  aria-label="embedding task"
                  value={draft.task ?? ""}
                  onChange={(e) => setDraft({ ...draft, task: e.target.value || null })}
                  disabled={formLocked}
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
              disabled={testing || formLocked}
              onClick={handleTest}
              className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted disabled:opacity-50 inline-flex items-center gap-1.5"
            >
              {testing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <PlugZap className="w-3.5 h-3.5" />}
              Test Connection
            </button>
            {dirty && (
              <button
                type="button"
                onClick={() => setDraft(draftFromAssignment(assignment, endpointById))}
                disabled={formLocked}
                className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted disabled:opacity-50"
              >
                Discard
              </button>
            )}
            <button
              type="button"
              disabled={!dirty || saving || formLocked}
              onClick={handleSave}
              className="text-xs px-4 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 font-medium"
            >
              {saving ? "Saving…" : "Save Changes"}
            </button>
          </div>
        </div>
      ) : null}

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

      {showHelp && <HelpDrawer onClose={() => setShowHelp(false)} />}

      {confirm && draft && (
        <MigrationConfirmModal
          factCount={factCount}
          targetProvider={provider || (chosenEndpoint?.preset ?? "")}
          targetModel={effModel}
          submitting={startingMigration}
          onCancel={() => setConfirm(null)}
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
          <button type="button" className="text-xs px-2 py-1 rounded-md hover:bg-muted" onClick={onClose} aria-label="Close help">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="text-xs space-y-3 text-muted-foreground">
          <HelpSection title="Jina v4 (default, multilingual)">
            2048-dim, ~$0.18 / 1M tokens. Strong multilingual recall — recommended for channels with
            Chinese / Japanese / mixed-language content.
          </HelpSection>
          <HelpSection title="OpenAI text-embedding-3-large">
            3072-dim, ~$0.13 / 1M tokens. The most popular default; multilingual; supports <code className="font-mono">dimensions=</code> resizing.
          </HelpSection>
          <HelpSection title="Voyage 3-large">
            1024-dim, ~$0.18 / 1M tokens. Strong English; multilingual; honors a <code className="font-mono">task=</code> kwarg similar to Jina.
          </HelpSection>
          <HelpSection title="Gemini (Google AI)">
            <code className="font-mono">text-embedding-004</code> 768d or <code className="font-mono">gemini-embedding-001</code> 3072d
            (Matryoshka-truncatable). Reuses your Google AI key; multilingual; per-request embeddings are currently free on the AI Studio key.
          </HelpSection>
          <HelpSection title="Ollama (local)">
            <code className="font-mono">nomic-embed-text</code> 768d / <code className="font-mono">mxbai-embed-large</code> 1024d (English-leaning), or
            <code className="font-mono"> bge-m3</code> / <code className="font-mono">snowflake-arctic-embed2</code> 1024d (multilingual) — all free, local. Best for self-hosted isolation.
          </HelpSection>
          <p>
            Self-hosted or proxy model not in the list? Pick the matching endpoint and choose
            <span className="font-medium"> "Other (custom model)…"</span> — the backend probes its dimension on first use.
          </p>
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

// ── salvaged: MigrationConfirmModal (cost-preview re-embed confirmation) ────

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

  // Escape closes — matches the rest of the Settings modals.
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "Escape") onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onCancel} aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Confirm re-embed"
        className="relative z-10 w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl"
      >
        <div className="px-5 py-4 border-b border-border flex items-center justify-between">
          <h3 className="text-sm font-semibold flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-500" />
            Re-embed everything
          </h3>
          <button type="button" onClick={onCancel} className="text-muted-foreground hover:text-foreground" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="px-5 py-4 space-y-3 text-sm">
          <p>
            This switches every stored fact + entity-name vector to{" "}
            <span className="font-mono">{targetProvider}/{targetModel}</span>. Search degrades to keyword-only
            (BM25) for a few minutes while the job runs; sync is paused during the window.
          </p>
          <div className="rounded-md border border-border bg-muted/30 px-3 py-2 text-xs space-y-1">
            <div>Facts to re-embed: <span className="font-mono">{factCount.toLocaleString()}</span></div>
            <div>
              Estimated cost:{" "}
              {spec?.local || (spec && spec.cost_per_m === 0) ? (
                <span className="font-mono">free (local)</span>
              ) : spec ? (
                <>
                  <span className="font-mono">{formatDollars(cost.dollars)}</span>{" "}
                  <span className="text-muted-foreground">({cost.tokens.toLocaleString()} tokens × ${spec.cost_per_m.toFixed(2)} / 1M)</span>
                </>
              ) : (
                <span className="text-muted-foreground">unknown — verify after Test Connection</span>
              )}
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Runs in the background; progress streams into the Activity feed and the banner above. Resumable on failure.
          </p>
        </div>
        <div className="px-5 py-3 border-t border-border flex justify-end gap-2 bg-muted/30 rounded-b-2xl">
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
