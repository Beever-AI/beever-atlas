import { useState } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleDot,
  Copy,
  ExternalLink,
  Info,
  KeyRound,
  Loader2,
  Pencil,
  PlugZap,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import type { Endpoint, PersistedModelKind } from "@/lib/aiSetup";
import { getEndpointPreset, modelsDocsUrl } from "@/lib/aiSetup";
import { getPresetIdentity } from "@/lib/endpointPresetIdentity";



/** Inline result of a Test Connection call, surfaced on the card. */
export interface EndpointTestResult {
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
  /** PR-β: which model the test actually probed (response-only). */
  probed_model?: string | null;
  probed_kind?: PersistedModelKind | null;
}

/** Inline result of a Discover Models call, surfaced on the card. */
export interface EndpointDiscoverResult {
  ok: boolean;
  count: number;
  error: string | null;
  /** PR-α: kept buckets (chat / embedding lists). Optional for pre-α backends. */
  by_kind?: { chat: string[]; embedding: string[] };
  /** PR-α: counts per dropped category (reranker / image_gen / …). */
  dropped_breakdown?: Record<string, number>;
}

/**
 * Pretty-print a dropped-category key for the breakdown tooltip:
 *   image_gen   → "image gen"
 *   audio_stt   → "audio STT"
 *   audio_synth → "audio synth"
 *   fine_tune   → "fine-tune"
 *   clip        → "CLIP / vision-only"
 *   vlm         → "VLM"
 *   reranker    → "reranker"
 *   moderation  → "moderation"
 *   reader      → "reader"
 *   segmenter   → "segmenter"
 *   other       → "other"
 */
export function prettyDroppedCategory(key: string): string {
  switch (key) {
    case "image_gen":
      return "image gen";
    case "audio_stt":
      return "audio STT";
    case "audio_synth":
      return "audio synth";
    case "fine_tune":
      return "fine-tune";
    case "clip":
      return "CLIP / vision-only";
    case "vlm":
      return "VLM";
    default:
      return key.replace(/_/g, " ");
  }
}

/** Build the breakdown tooltip string from `dropped_breakdown` (non-zero entries only). */
export function buildBreakdownTooltip(breakdown: Record<string, number> | undefined): string {
  if (!breakdown) return "";
  const parts: string[] = [];
  for (const [key, count] of Object.entries(breakdown)) {
    if (!count || count <= 0) continue;
    parts.push(`${count} ${prettyDroppedCategory(key)}`);
  }
  return parts.join(" · ");
}

interface EndpointCardProps {
  endpoint: Endpoint;
  /** Number of assignments (primary + fallback) pointing at this endpoint. */
  usedByCount: number;
  /** Consumer names referencing this endpoint — shown in the "used by" tooltip. */
  usedByConsumers?: string[];
  /** True while a Test / Discover / Delete / model-edit on this endpoint is in flight. */
  busy?: boolean;
  /** Most recent test result (set by the parent after calling onTest). */
  testResult?: EndpointTestResult | null;
  /** Most recent discover result (set by the parent after calling onDiscover). */
  discoverResult?: EndpointDiscoverResult | null;
  /** Opens the edit modal (rendered by the parent at page level). */
  onEdit?: () => void;
  onTest: () => void;
  onDiscover: () => void;
  onDelete: () => void;
  /**
   * Persist the model list immediately — wired by the parent to
   * ``ep.update(id, { models })``. Called when a model chip's ✕ is clicked or a
   * model is added via the inline "+ add model" affordance.
   */
  onUpdateModels?: (models: string[]) => Promise<void>;
  /**
   * PR-γ: promote one of ``endpoint.advanced_models`` into ``endpoint.models[]``
   * by appending it to ``endpoint.manually_kept`` and persisting via
   * ``ep.update(id, { manually_kept: […, model] })``. The backend's Discover
   * already preserves ``manually_kept`` across re-Discover, so the promoted
   * model survives the next refresh.
   */
  onPromoteAdvanced?: (model: string) => Promise<void>;
}

interface StatusPill {
  Icon: typeof CircleDot;
  /** Tailwind classes for the badge (bg + ring + text). */
  cls: string;
  label: string;
}

function computeStatus(e: Endpoint, testResult: EndpointTestResult | null | undefined): StatusPill {
  if (e.has_credential === false && e.auth_type !== "none") {
    return {
      Icon: CircleDot,
      cls: "bg-muted text-muted-foreground ring-border",
      label: "no key",
    };
  }
  if (testResult) {
    return testResult.ok
      ? {
          Icon: CheckCircle2,
          cls: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 ring-emerald-500/30",
          label: `connected · ${testResult.latency_ms}ms`,
        }
      : {
          Icon: AlertTriangle,
          cls: "bg-destructive/10 text-destructive ring-destructive/30",
          label: `failed: ${testResult.error ?? "unknown"}`,
        };
  }
  if (e.last_test_ok === true) {
    return {
      Icon: CheckCircle2,
      cls: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 ring-emerald-500/30",
      label: "tested ok",
    };
  }
  if (e.last_test_ok === false) {
    return {
      Icon: AlertTriangle,
      cls: "bg-destructive/10 text-destructive ring-destructive/30",
      label: e.last_test_error ?? "last test failed",
    };
  }
  return {
    Icon: CircleDot,
    cls: "bg-amber-500/10 text-amber-600 dark:text-amber-400 ring-amber-500/30",
    label: "untested",
  };
}

/**
 * An endpoint auto-created by the env-hydration shim gets a noisy name like
 * ``google_ai (from GOOGLE_API_KEY)`` and the ``migrated-from-env`` tag. Return
 * a clean display name + (when auto-generated) the env var it came from so the
 * card can show a discreet "auto-detected from `…`" badge instead of polluting
 * the title.
 */
function resolveDisplayName(e: Endpoint): { title: string; envVar: string | null } {
  const autoPrefix = `${e.preset} (from `;
  const fromEnvName = e.name.startsWith(autoPrefix) && e.name.endsWith(")");
  const taggedFromEnv = e.tags.includes("migrated-from-env");
  if (fromEnvName || taggedFromEnv) {
    const friendly = getEndpointPreset(e.preset)?.label ?? e.preset;
    let envVar: string | null = null;
    const m = e.name.match(/\(from\s+([^)]+)\)/);
    if (m) envVar = m[1].trim();
    return { title: friendly, envVar };
  }
  return { title: e.name, envVar: null };
}

/** Pretty-print a base URL down to its hostname; fall back to the raw string. */
function shortHost(baseUrl: string): string {
  if (!baseUrl) return "";
  try {
    return new URL(baseUrl).host || baseUrl;
  } catch {
    return baseUrl;
  }
}

/**
 * Presentational card for one Endpoint. The parent owns the ``useEndpoints``
 * hook plus the edit/test/discover/delete/model-edit handlers; this component
 * just renders state. Calm layout: a tinted per-preset icon box; a clean
 * display name (+ an "auto-detected from `ENV`" badge for env-hydrated
 * endpoints) and the family chip; a rounded status badge top-right; the
 * endpoint host on its own line with a copy button; an in-place editable model
 * list (chips with ✕, an "+ add model" input, the Discover action); and an
 * Edit / Test / Discover · Delete button row. Editing the endpoint itself
 * happens in a modal the parent renders — the card just calls ``onEdit``.
 */
export function EndpointCard({
  endpoint: e,
  usedByCount,
  usedByConsumers,
  busy = false,
  testResult,
  discoverResult,
  onEdit,
  onTest,
  onDiscover,
  onDelete,
  onUpdateModels,
  onPromoteAdvanced,
}: EndpointCardProps) {
  const status = computeStatus(e, testResult);
  const identity = getPresetIdentity(e.preset);
  const { Icon } = identity;
  const modelCount = e.models.length;
  const { title: displayName, envVar } = resolveDisplayName(e);

  // Expanded-by-default for short lists; collapsed when there's a lot.
  const [showModels, setShowModels] = useState(modelCount > 0 && modelCount <= 8);
  const [newModel, setNewModel] = useState("");
  const [copied, setCopied] = useState(false);
  // PR-γ: collapsible panel for the dropped/advanced models from Discover.
  const [showAdvanced, setShowAdvanced] = useState(false);
  // Tracks which advanced model is currently being promoted (to disable that
  // chip's button + show a spinner without locking the rest of the card).
  const [promotingModel, setPromotingModel] = useState<string | null>(null);

  const usedByTitle =
    usedByCount === 0
      ? "Not used by any agent or the embedding model yet"
      : `Used by: ${
          usedByConsumers && usedByConsumers.length > 0
            ? usedByConsumers.join(", ")
            : `${usedByCount} consumer${usedByCount === 1 ? "" : "s"}`
        }`;

  const host = shortHost(e.base_url);

  async function removeModel(m: string) {
    if (!onUpdateModels) return;
    await onUpdateModels(e.models.filter((x) => x !== m));
  }

  async function addModel() {
    const v = newModel.trim();
    if (!v || !onUpdateModels) return;
    if (e.models.includes(v)) {
      setNewModel("");
      return;
    }
    await onUpdateModels([...e.models, v]);
    setNewModel("");
  }

  function copyBaseUrl() {
    if (!e.base_url) return;
    void navigator.clipboard?.writeText(e.base_url);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1000);
  }

  async function promoteAdvanced(model: string) {
    if (!onPromoteAdvanced) return;
    setPromotingModel(model);
    try {
      await onPromoteAdvanced(model);
    } finally {
      setPromotingModel(null);
    }
  }

  // PR-γ: derive the rich Discover summary. Only render when the backend
  // returned the new ``by_kind`` shape — pre-α endpoints fall back to the
  // legacy "Discovered N models — added" line.
  const hasRichDiscover =
    discoverResult?.ok === true && discoverResult.by_kind !== undefined;
  const richKept = hasRichDiscover
    ? (discoverResult!.by_kind!.chat.length + discoverResult!.by_kind!.embedding.length)
    : 0;
  const richDroppedCount = hasRichDiscover && discoverResult!.dropped_breakdown
    ? Object.values(discoverResult!.dropped_breakdown).reduce((a, b) => a + b, 0)
    : 0;
  const richChatCount = hasRichDiscover ? discoverResult!.by_kind!.chat.length : 0;
  const richEmbeddingCount = hasRichDiscover ? discoverResult!.by_kind!.embedding.length : 0;
  const breakdownTooltip = hasRichDiscover
    ? buildBreakdownTooltip(discoverResult!.dropped_breakdown)
    : "";

  const advancedModels = e.advanced_models ?? [];

  return (
    <div className="group rounded-xl border border-border bg-card overflow-hidden transition-shadow hover:shadow-md hover:shadow-black/5 dark:hover:shadow-black/20">
      {/* Header strip */}
      <div className="px-4 py-3 border-b border-border flex items-start gap-3">
        <div
          className={`w-10 h-10 rounded-lg flex items-center justify-center shrink-0 ${identity.iconBox}`}
        >
          <Icon className="w-5 h-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-foreground truncate">{displayName}</span>
            <span
              className={`inline-flex items-center rounded-md px-1.5 py-0.5 text-[10px] font-medium ${identity.chip}`}
            >
              {identity.family}
            </span>
          </div>
          <div className="flex items-center gap-2 flex-wrap mt-0.5 text-xs text-muted-foreground">
            {envVar && (
              <span className="inline-flex items-center gap-1" title={`Auto-detected from the ${envVar} environment variable`}>
                <KeyRound className="h-3 w-3 shrink-0" />
                auto-detected from <code className="font-mono">{envVar}</code>
              </span>
            )}
            {e.has_credential ? (
              <span className="font-mono truncate" title={e.credential_masked}>
                {e.credential_masked}
              </span>
            ) : (
              <span className="italic">{e.auth_type === "none" ? "no auth" : "no credential"}</span>
            )}
          </div>
        </div>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 shrink-0 ${status.cls}`}
          title={status.label}
        >
          <status.Icon className="h-3 w-3" />
          <span className="max-w-[10rem] truncate">{status.label}</span>
        </span>
      </div>

      {/* Body */}
      <div className="px-4 py-3 space-y-3">
        {/* Endpoint host — own line, copy-to-clipboard */}
        <div className="flex items-center gap-2 min-w-0">
          {e.base_url ? (
            <>
              <span
                className="text-xs font-mono text-muted-foreground truncate"
                title={e.base_url}
              >
                {host}
              </span>
              <button
                type="button"
                onClick={copyBaseUrl}
                className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground shrink-0"
                title={copied ? "Copied" : `Copy ${e.base_url}`}
                aria-label="Copy endpoint URL"
              >
                {copied ? (
                  <>
                    <Check className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
                    <span className="text-emerald-600 dark:text-emerald-400">copied</span>
                  </>
                ) : (
                  <Copy className="h-3 w-3" />
                )}
              </button>
            </>
          ) : (
            <span className="text-xs text-muted-foreground italic">(no base URL — set in Edit)</span>
          )}
        </div>

        {/* Models section */}
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => modelCount > 0 && setShowModels((v) => !v)}
              disabled={modelCount === 0}
              className={`inline-flex items-center gap-1 text-[11px] font-medium ${
                modelCount === 0
                  ? "text-muted-foreground cursor-default opacity-70"
                  : "text-muted-foreground hover:text-foreground"
              }`}
              aria-expanded={modelCount > 0 ? showModels : undefined}
              title={modelCount === 0 ? "No models — run Discover" : `${modelCount} models`}
            >
              {`${modelCount} ${modelCount === 1 ? "model" : "models"}`}
              {modelCount > 0 &&
                (showModels ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />)}
            </button>
            {usedByCount > 0 && (
              <span className="text-[11px] text-muted-foreground" title={usedByTitle}>
                · used by {usedByCount} {usedByCount === 1 ? "agent" : "agents"}
              </span>
            )}
            {/* PR-κ: link to the provider's official model docs so operators
                can see what's available before typing a model id by hand. */}
            <a
              href={modelsDocsUrl(e.preset)}
              target="_blank"
              rel="noreferrer noopener"
              className="ml-auto inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
              title={`Open ${e.preset} model catalog in a new tab`}
            >
              View available models
              <ExternalLink className="h-3 w-3" />
            </a>
          </div>

          {showModels && (
            <div className="flex flex-wrap items-center gap-1">
              {e.models.map((m) => (
                <span
                  key={m}
                  className="inline-flex items-center gap-1 rounded bg-muted pl-1.5 pr-1 py-0.5 text-xs font-mono text-muted-foreground"
                >
                  {m}
                  {onUpdateModels && (
                    <button
                      type="button"
                      onClick={() => void removeModel(m)}
                      disabled={busy}
                      className="rounded-sm p-0.5 hover:bg-destructive/15 hover:text-destructive disabled:opacity-50"
                      aria-label={`Remove model ${m}`}
                      title={`Remove ${m}`}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  )}
                </span>
              ))}
              {onUpdateModels && (
                <span className="inline-flex items-center gap-1 rounded border border-dashed border-border pl-1.5 pr-1 py-0.5">
                  <input
                    value={newModel}
                    onChange={(ev) => setNewModel(ev.target.value)}
                    onKeyDown={(ev) => {
                      if (ev.key === "Enter") {
                        ev.preventDefault();
                        void addModel();
                      }
                    }}
                    disabled={busy}
                    placeholder="add model…"
                    aria-label="Add a model"
                    className="w-28 bg-transparent text-xs font-mono outline-none placeholder:text-muted-foreground/60 disabled:opacity-50"
                  />
                  <button
                    type="button"
                    onClick={() => void addModel()}
                    disabled={busy || !newModel.trim()}
                    className="rounded-sm p-0.5 text-muted-foreground hover:bg-primary/10 hover:text-primary disabled:opacity-40"
                    aria-label="Add model"
                    title="Add model"
                  >
                    {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
                  </button>
                </span>
              )}
            </div>
          )}
          {modelCount === 0 && (
            <div className="text-[11px] text-muted-foreground italic">
              (no models — add one above or run Discover)
            </div>
          )}
        </div>

        {/* Button row */}
        <div className="flex items-center gap-2 pt-0.5">
          {onEdit && (
            <button
              type="button"
              onClick={onEdit}
              disabled={busy}
              className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md border border-border hover:bg-muted disabled:opacity-50"
            >
              <Pencil className="h-3 w-3" /> Edit
            </button>
          )}
          <button
            type="button"
            onClick={onTest}
            disabled={busy}
            className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md border border-border hover:bg-muted disabled:opacity-50"
          >
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <PlugZap className="h-3 w-3" />} Test
          </button>
          <button
            type="button"
            onClick={onDiscover}
            disabled={busy}
            className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md border border-border hover:bg-muted disabled:opacity-50"
          >
            <RefreshCw className="h-3 w-3" /> Discover
          </button>
          <button
            type="button"
            onClick={onDelete}
            disabled={busy}
            className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-md border border-border text-destructive hover:bg-destructive/10 disabled:opacity-50 ml-auto"
          >
            {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />} Delete
          </button>
        </div>

        {/* Inline test / discover results */}
        {testResult && (
          <div
            className={`text-xs flex items-center gap-1.5 ${
              testResult.ok ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"
            }`}
          >
            {testResult.ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <AlertTriangle className="h-3.5 w-3.5" />}
            {testResult.ok
              ? testResult.probed_model
                ? `Test passed (probed ${testResult.probed_model}, ${testResult.latency_ms} ms)`
                : `Connected · ${testResult.latency_ms}ms`
              : `Test failed: ${testResult.error ?? "unknown"}`}
          </div>
        )}
        {discoverResult && !hasRichDiscover && (
          <div
            className={`text-xs flex items-center gap-1.5 ${
              discoverResult.ok ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"
            }`}
          >
            {discoverResult.ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <AlertTriangle className="h-3.5 w-3.5" />}
            {discoverResult.ok
              ? `Discovered ${discoverResult.count} models — added`
              : `Discover failed: ${discoverResult.error ?? "unknown"}`}
          </div>
        )}

        {/* PR-γ: rich Discover summary card. Shows kept/filtered counts, a
            tooltip with the per-category breakdown, and a [Show advanced]
            toggle that reveals the dropped-model chips with "+ Promote"
            buttons. Only rendered when the backend returned ``by_kind``. */}
        {hasRichDiscover && (
          <div className="rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 space-y-1.5">
            <div className="flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-400">
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
              <span className="font-medium">
                Discovered {richKept} {richKept === 1 ? "model" : "models"}
                {richDroppedCount > 0 && (
                  <span className="text-muted-foreground"> · {richDroppedCount} filtered</span>
                )}
              </span>
              {richDroppedCount > 0 && breakdownTooltip && (
                <span
                  className="text-muted-foreground inline-flex"
                  title={breakdownTooltip}
                  aria-label={`Breakdown: ${breakdownTooltip}`}
                >
                  <Info className="h-3.5 w-3.5" />
                </span>
              )}
            </div>
            <div className="text-[11px] text-muted-foreground pl-5">
              {richChatCount} chat · {richEmbeddingCount} embedding
              {richDroppedCount > 0 && ` · ${richDroppedCount} filtered out`}
            </div>
            {advancedModels.length > 0 && (
              <div className="pl-5 pt-0.5">
                <button
                  type="button"
                  onClick={() => setShowAdvanced((v) => !v)}
                  className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground hover:text-foreground"
                  aria-expanded={showAdvanced}
                  aria-controls={`advanced-models-${e.id}`}
                >
                  {showAdvanced ? "Hide advanced" : "Show advanced"}
                  {showAdvanced ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                </button>
              </div>
            )}
            {showAdvanced && advancedModels.length > 0 && (
              <div id={`advanced-models-${e.id}`} className="pl-5 pt-1 space-y-1">
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground font-medium">
                  Advanced — filtered by the classifier
                </div>
                <div className="flex flex-wrap items-center gap-1">
                  {advancedModels.map((m) => (
                    <span
                      key={m}
                      className="inline-flex items-center gap-1 rounded bg-muted/70 pl-1.5 pr-1 py-0.5 text-xs font-mono text-muted-foreground border border-border/50"
                    >
                      {m}
                      {onPromoteAdvanced && (
                        <button
                          type="button"
                          onClick={() => void promoteAdvanced(m)}
                          disabled={busy || promotingModel === m}
                          className="inline-flex items-center gap-0.5 rounded-sm px-1 py-px text-[10px] font-medium text-primary hover:bg-primary/10 disabled:opacity-50"
                          aria-label={`Promote ${m} to the model list`}
                          title={`Promote ${m} to the model list`}
                        >
                          {promotingModel === m ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            <Sparkles className="h-3 w-3" />
                          )}
                          Promote
                        </button>
                      )}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
