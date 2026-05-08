import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  Globe,
  HardDrive,
  Info,
  KeyRound,
  Loader2,
  PlugZap,
  X,
} from "lucide-react";
import { useEmbeddingSettings } from "@/hooks/useEmbeddingSettings";
import {
  PROVIDER_DEFAULT_MODEL,
  PROVIDER_KEY_ENV,
  PROVIDER_LABELS,
  SUPPORTED_PROVIDERS,
  estimateMigrationCost,
  formatCost,
  lookupModel,
  modelsForProvider,
} from "@/lib/knownEmbeddingModels";
import { ProviderTile } from "./ProviderTile";
import type {
  EmbeddingMigrationStatus,
  EmbeddingProbeResult,
  EmbeddingSettings as EmbeddingSettingsType,
  EmbeddingUpdateRequest,
} from "@/lib/types";

type DraftState = Pick<
  EmbeddingSettingsType,
  "provider" | "model" | "dimensions" | "rpm" | "api_base" | "task"
> & {
  api_key: string | null; // null = no change; "" = clear; "<plaintext>" = set
};

function draftFromConfig(cfg: EmbeddingSettingsType): DraftState {
  return {
    provider: cfg.provider,
    model: cfg.model,
    dimensions: cfg.dimensions,
    rpm: cfg.rpm,
    api_base: cfg.api_base,
    task: cfg.task,
    api_key: null,
  };
}

function isDirty(cfg: EmbeddingSettingsType, draft: DraftState): boolean {
  return (
    draft.provider !== cfg.provider ||
    draft.model !== cfg.model ||
    draft.dimensions !== cfg.dimensions ||
    draft.rpm !== cfg.rpm ||
    draft.api_base !== cfg.api_base ||
    draft.task !== cfg.task ||
    draft.api_key !== null
  );
}

export function EmbeddingSettings() {
  const { config, isLoading, error, refetch, update, test, startMigration, getMigrationStatus } =
    useEmbeddingSettings();

  const [draft, setDraft] = useState<DraftState | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<EmbeddingProbeResult | null>(null);
  const [showHelp, setShowHelp] = useState(false);
  const [replaceKey, setReplaceKey] = useState(false);
  const [migrationStatus, setMigrationStatus] = useState<EmbeddingMigrationStatus | null>(null);
  const [confirmMigrationOpen, setConfirmMigrationOpen] = useState(false);
  const [factCountForCost, setFactCountForCost] = useState<number | null>(null);

  useEffect(() => {
    if (config && !draft) setDraft(draftFromConfig(config));
  }, [config, draft]);

  // Poll migration status while running.
  useEffect(() => {
    let timer: number | undefined;
    async function poll() {
      try {
        const status = await getMigrationStatus();
        setMigrationStatus(status);
        if (status.running) {
          timer = window.setTimeout(poll, 2000);
        }
      } catch {
        // best-effort polling; surface error in UI on the next refetch.
      }
    }
    poll();
    return () => {
      if (timer) window.clearTimeout(timer);
    };
  }, [getMigrationStatus]);

  if (isLoading && !config) {
    return (
      <div className="text-sm text-muted-foreground py-8 text-center">
        Loading embedding configuration…
      </div>
    );
  }
  if (!config || !draft) return null;

  const currentSpec = lookupModel(draft.provider, draft.model);
  const dirty = isDirty(config, draft);
  const dimMismatchPersisted =
    config.last_probe_at && config.dimensions !== draft.dimensions;

  function handleProvider(value: string) {
    if (!draft) return;
    // Prefer the curated default model for the provider; otherwise fall
    // back to the first model in the known-models table.
    const preferred = PROVIDER_DEFAULT_MODEL[value as keyof typeof PROVIDER_DEFAULT_MODEL];
    const fallbackList = modelsForProvider(value);
    const nextModel = preferred ?? fallbackList[0] ?? "";
    const nextSpec = nextModel ? lookupModel(value, nextModel) : null;
    setDraft({
      ...draft,
      provider: value as DraftState["provider"],
      model: nextModel,
      dimensions: nextSpec?.dim ?? draft.dimensions,
    });
    setTestResult(null);
  }

  function handleModel(model: string) {
    if (!draft) return;
    const spec = lookupModel(draft.provider, model);
    setDraft({
      ...draft,
      model,
      dimensions: spec?.dim ?? draft.dimensions,
    });
    setTestResult(null);
  }

  async function handleTest() {
    if (!draft) return;
    setTesting(true);
    setTestResult(null);
    try {
      const req: EmbeddingUpdateRequest = {
        provider: draft.provider,
        model: draft.model,
        dimensions: draft.dimensions,
        rpm: draft.rpm,
        api_base: draft.api_base,
        task: draft.task,
      };
      if (draft.api_key !== null) req.api_key = draft.api_key;
      const result = await test(req);
      setTestResult(result);
    } catch (err: any) {
      setTestResult({
        ok: false,
        dimensions: null,
        latency_ms: null,
        provider: draft.provider,
        model: draft.model,
        error: err?.message ?? "Test failed",
      });
    } finally {
      setTesting(false);
    }
  }

  async function handleSave(force?: boolean) {
    if (!draft) return;
    setSaving(true);
    try {
      const req: EmbeddingUpdateRequest = {
        provider: draft.provider,
        model: draft.model,
        dimensions: draft.dimensions,
        rpm: draft.rpm,
        api_base: draft.api_base,
        task: draft.task,
      };
      if (draft.api_key !== null) req.api_key = draft.api_key;
      if (force) req.confirm_migration = true;
      const updated = await update(req);
      setDraft(draftFromConfig(updated));
      setReplaceKey(false);
    } catch (err: any) {
      const detail = err?.detail ?? err;
      if (detail?.error === "dim_mismatch_requires_migration") {
        setFactCountForCost(detail.fact_count ?? 0);
        setConfirmMigrationOpen(true);
        return;
      }
      throw err;
    } finally {
      setSaving(false);
    }
  }

  async function handleStartMigration() {
    // Save with confirm_migration first, then spawn the job.
    await handleSave(true);
    await startMigration();
    const status = await getMigrationStatus();
    setMigrationStatus(status);
    setConfirmMigrationOpen(false);
    refetch();
  }

  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-primary/10 text-primary flex items-center justify-center">
            <PlugZap className="w-4 h-4" />
          </div>
          <div>
            <div className="text-sm font-semibold text-foreground">Embedding Model</div>
            <div className="text-xs text-muted-foreground">
              {PROVIDER_LABELS[draft.provider] ?? draft.provider} · {draft.model} · {draft.dimensions}d
              {config.source !== "default" && (
                <span className="ml-2 px-1.5 py-0.5 rounded bg-muted/60 border border-border text-[10px] uppercase tracking-wide">
                  {config.source === "env" ? "from .env" : "saved"}
                </span>
              )}
            </div>
          </div>
        </div>
        <button
          type="button"
          onClick={() => setShowHelp((v) => !v)}
          className="text-xs px-2 py-1 rounded-md text-muted-foreground hover:bg-muted"
        >
          <Info className="w-3.5 h-3.5 inline mr-1" />
          What's the difference?
        </button>
      </div>

      {error && (
        <div className="text-xs text-destructive bg-destructive/10 px-4 py-2">{error}</div>
      )}

      {/* Migration banner — shown when a job is running. Includes a live
          progress bar + ETA. The activity-feed surface in the dashboard
          gets the same stage_output entries from the reembed_state doc. */}
      {migrationStatus?.running && (() => {
        const processed = migrationStatus.processed ?? 0;
        const total = migrationStatus.total ?? 0;
        const pct = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : 0;
        // ETA: rough proportional projection — assume linear progress from
        // the start of the run. Hides when we have no progress yet.
        const startedAt = migrationStatus.started_at
          ? Date.parse(migrationStatus.started_at)
          : null;
        const elapsedSec = startedAt ? (Date.now() - startedAt) / 1000 : 0;
        const etaMin =
          processed > 0 && total > processed
            ? Math.max(1, Math.round(((total - processed) / processed) * elapsedSec / 60))
            : null;
        return (
          <div className="px-4 py-3 bg-amber-500/10 border-b border-amber-500/30 flex items-start gap-3">
            <Loader2 className="w-4 h-4 animate-spin text-amber-600 dark:text-amber-400 mt-0.5" />
            <div className="text-xs flex-1 space-y-1.5">
              <div className="font-medium text-amber-700 dark:text-amber-300 flex items-center justify-between gap-2">
                <span>Re-embedding in progress · {migrationStatus.stage ?? "starting"}</span>
                <span className="text-amber-600/80 dark:text-amber-400/80 font-mono">
                  {pct}%{etaMin != null ? ` · ETA ${etaMin}m` : ""}
                </span>
              </div>
              {total > 0 && (
                <div className="h-1.5 rounded-full bg-amber-500/20 overflow-hidden">
                  <div
                    className="h-full bg-amber-500 transition-all duration-500"
                    style={{ width: `${pct}%` }}
                  />
                </div>
              )}
              <div className="text-amber-600/80 dark:text-amber-400/80">
                {processed.toLocaleString()} / {total.toLocaleString()} rows ·
                Search degraded to keyword-only · Sync paused
              </div>
            </div>
          </div>
        );
      })()}

      {dimMismatchPersisted && !migrationStatus?.running && (
        <div className="px-4 py-3 bg-amber-500/10 border-b border-amber-500/30 flex items-start gap-3">
          <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-400 mt-0.5" />
          <div className="text-xs text-amber-700 dark:text-amber-300">
            Saving a different dimension requires a re-embed migration. The dim guard will
            refuse to start the backend until you run it.{" "}
            <a
              href="https://github.com/Beever-AI/beever-atlas/blob/main/docs/runbooks/embedding-migration.md"
              target="_blank"
              rel="noreferrer"
              className="underline"
            >
              View runbook
            </a>
          </div>
        </div>
      )}

      <div className="p-4 space-y-4">
        {/* Pill row */}
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {currentSpec ? (
            <>
              <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border ${
                currentSpec.multilingual
                  ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/30"
                  : "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/30"
              }`}>
                <Globe className="w-3 h-3" />
                {currentSpec.multilingual ? "Multilingual" : "English-leaning"}
              </span>
              <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-muted text-muted-foreground border border-border">
                {formatCost(currentSpec)}
              </span>
              <span className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border ${
                currentSpec.local
                  ? "bg-sky-500/10 text-sky-600 dark:text-sky-400 border-sky-500/30"
                  : "bg-muted text-muted-foreground border-border"
              }`}>
                <HardDrive className="w-3 h-3" />
                {currentSpec.local ? "Local" : "Cloud"}
              </span>
            </>
          ) : (
            <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-muted text-muted-foreground border border-border">
              <CircleDot className="w-3 h-3" />
              Custom model — verify with Test Connection
            </span>
          )}
        </div>

        {/* Provider tile grid (replaces the previous dropdown) */}
        <div>
          <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">
            Choose provider
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
            {SUPPORTED_PROVIDERS.map((p) => (
              <ProviderTile
                key={p}
                provider={p}
                selected={draft.provider === p}
                onClick={() => handleProvider(p)}
              />
            ))}
          </div>
        </div>

        {/* Pre-switch inline warning — fires the moment the operator picks
            a provider/dim different from what's saved AND search would
            require a re-embed migration. Set expectations BEFORE Save. */}
        {(() => {
          const dimChanged = draft.dimensions !== config.dimensions;
          const providerChanged = draft.provider !== config.provider;
          const hasSavedConfig = config.source !== "default" || config.last_probe_at;
          const showPreSwitchWarning =
            (providerChanged || dimChanged) &&
            hasSavedConfig &&
            !migrationStatus?.running;
          if (!showPreSwitchWarning) return null;
          const warnSpec = lookupModel(draft.provider, draft.model);
          return (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300 flex gap-2">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <div className="space-y-1">
                <div className="font-medium">
                  Heads up — switching from{" "}
                  <span className="font-mono">
                    {config.provider}/{config.model}
                  </span>{" "}
                  to{" "}
                  <span className="font-mono">
                    {draft.provider}/{draft.model}
                  </span>{" "}
                  will require re-embedding all stored facts.
                </div>
                <div>
                  Search will degrade to keyword-only (BM25) for ~5–15 min while
                  the migration runs. Sync is paused during the window. Cost is
                  shown on Save Changes — typically &lt; $1 for OSS-scale data
                  {warnSpec?.local ? " (free for local providers)" : ""}.
                </div>
              </div>
            </div>
          );
        })()}

        {/* Model + dimensions row */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">Model</span>
            <input
              list={`emb-models-${draft.provider}`}
              className="text-sm bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
              value={draft.model}
              onChange={(e) => handleModel(e.target.value)}
              placeholder="e.g. text-embedding-3-large"
            />
            <datalist id={`emb-models-${draft.provider}`}>
              {modelsForProvider(draft.provider).map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">
              Dimensions{" "}
              {currentSpec && (
                <span className="text-[10px] uppercase tracking-wide text-muted-foreground/70">
                  · auto
                </span>
              )}
            </span>
            <input
              type="number"
              min={1}
              max={8192}
              className="text-sm bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
              value={draft.dimensions}
              onChange={(e) =>
                setDraft({ ...draft, dimensions: Number(e.target.value) || 0 })
              }
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">RPM</span>
            <input
              type="number"
              min={1}
              max={10000}
              className="text-sm bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
              value={draft.rpm}
              onChange={(e) => setDraft({ ...draft, rpm: Number(e.target.value) || 0 })}
            />
          </label>

          <label className="flex flex-col gap-1.5 md:col-span-2">
            <span className="text-xs font-medium text-muted-foreground">
              API Base (optional override)
            </span>
            <input
              type="url"
              placeholder="leave blank for provider default"
              className="text-sm bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30"
              value={draft.api_base}
              onChange={(e) => setDraft({ ...draft, api_base: e.target.value })}
            />
          </label>
        </div>

        {/* API key */}
        <div className="space-y-1.5">
          <div className="text-xs font-medium text-muted-foreground flex items-center gap-1.5">
            <KeyRound className="w-3 h-3" />
            API Key
            <span className="text-muted-foreground/60">
              · provider env: {PROVIDER_KEY_ENV[draft.provider]}
            </span>
          </div>
          {!replaceKey && config.has_api_key && (
            <div className="flex items-center gap-2">
              <input
                readOnly
                className="text-sm flex-1 bg-muted/50 border border-border rounded-md px-3 py-2 text-muted-foreground font-mono"
                value={config.api_key_masked}
              />
              <button
                type="button"
                className="text-xs px-3 py-2 rounded-md border border-border hover:bg-muted"
                onClick={() => {
                  setReplaceKey(true);
                  setDraft({ ...draft, api_key: "" });
                }}
              >
                Replace
              </button>
            </div>
          )}
          {(replaceKey || !config.has_api_key) && (
            <div className="flex items-center gap-2">
              <input
                type="password"
                placeholder="Paste new API key (leave blank to use provider env var)"
                className="text-sm flex-1 bg-background border border-border rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/30 font-mono"
                value={draft.api_key ?? ""}
                onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
              />
              {replaceKey && (
                <button
                  type="button"
                  className="text-xs px-2 py-2 rounded-md border border-border hover:bg-muted"
                  onClick={() => {
                    setReplaceKey(false);
                    setDraft({ ...draft, api_key: null });
                  }}
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          )}
        </div>

        {/* Test result + actions */}
        {testResult && (
          <div
            className={`rounded-md border px-3 py-2 text-xs ${
              testResult.ok
                ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/30"
                : "bg-destructive/10 text-destructive border-destructive/30"
            }`}
          >
            {testResult.ok ? (
              <>
                <CheckCircle2 className="w-3.5 h-3.5 inline mr-1" />
                Test passed — provider returned {testResult.dimensions}-dim vector in{" "}
                {testResult.latency_ms} ms.
              </>
            ) : (
              <>
                <AlertTriangle className="w-3.5 h-3.5 inline mr-1" />
                Test failed: {testResult.error}
              </>
            )}
          </div>
        )}

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
              onClick={() => setDraft(draftFromConfig(config))}
              className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted"
            >
              Discard
            </button>
          )}
          <button
            type="button"
            disabled={!dirty || saving}
            onClick={() => handleSave(false)}
            className="text-xs px-4 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 font-medium"
          >
            {saving ? "Saving…" : "Save Changes"}
          </button>
        </div>
      </div>

      {showHelp && <HelpDrawer onClose={() => setShowHelp(false)} />}

      {confirmMigrationOpen && (
        <MigrationConfirmModal
          factCount={factCountForCost ?? 0}
          targetProvider={draft.provider}
          targetModel={draft.model}
          onCancel={() => setConfirmMigrationOpen(false)}
          onConfirm={handleStartMigration}
        />
      )}
    </div>
  );
}

function HelpDrawer({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-40 bg-background/60 backdrop-blur-sm flex justify-end">
      <div className="w-full max-w-md h-full bg-card border-l border-border overflow-y-auto p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">Picking an embedding provider</h3>
          <button
            type="button"
            className="text-xs px-2 py-1 rounded-md hover:bg-muted"
            onClick={onClose}
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="text-xs space-y-3 text-muted-foreground">
          <Section title="Jina v4 (default, multilingual)">
            2048-dim, ~$0.18 / 1M tokens. Strong multilingual recall — recommended for
            channels with Chinese / Japanese / mixed-language content.
          </Section>
          <Section title="OpenAI text-embedding-3-large">
            3072-dim, ~$0.13 / 1M tokens. The most popular default; multilingual; supports
            ``dimensions=`` resizing for cost-tuning.
          </Section>
          <Section title="Voyage 3-large">
            1024-dim, ~$0.18 / 1M tokens. Strong English; multilingual; honors a ``task=``
            kwarg similar to Jina.
          </Section>
          <Section title="Cohere v3 (multilingual)">
            1024-dim, ~$0.10 / 1M tokens. ``input_type`` parameter for retrieval vs.
            classification.
          </Section>
          <Section title="Gemini text-embedding-004">
            768-dim, $0.025 / 1M tokens. Cheapest cloud option; multilingual.
          </Section>
          <Section title="Ollama (local)">
            ``nomic-embed-text`` 768d / ``mxbai-embed-large`` 1024d — free, local. Quality
            varies by model and is generally English-leaning. Best for self-hosted
            isolation requirements; not recommended for multilingual channels.
          </Section>
          <p>
            Switching providers on a populated install requires a one-time re-embed —
            see the migration runbook from the banner above.
          </p>
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-foreground font-medium mb-0.5">{title}</div>
      <div>{children}</div>
    </div>
  );
}

function MigrationConfirmModal({
  factCount,
  targetProvider,
  targetModel,
  onCancel,
  onConfirm,
}: {
  factCount: number;
  targetProvider: string;
  targetModel: string;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}) {
  const spec = useMemo(
    () => lookupModel(targetProvider, targetModel),
    [targetProvider, targetModel],
  );
  const cost = useMemo(() => estimateMigrationCost(factCount, spec), [factCount, spec]);
  const [submitting, setSubmitting] = useState(false);

  return (
    <div className="fixed inset-0 z-50 bg-background/60 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="w-full max-w-md bg-card border border-border rounded-xl shadow-xl">
        <div className="px-5 py-4 border-b border-border flex items-center justify-between">
          <h3 className="text-sm font-semibold flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-500" />
            Re-embed migration required
          </h3>
          <button
            type="button"
            onClick={onCancel}
            className="text-muted-foreground hover:text-foreground"
          >
            <ChevronDown className="rotate-90 w-4 h-4" />
          </button>
        </div>
        <div className="px-5 py-4 space-y-3 text-sm">
          <p>
            Switching to <span className="font-mono">{targetProvider}/{targetModel}</span>{" "}
            changes the vector dimensionality. Atlas must re-embed every stored fact and
            entity name vector before search will work.
          </p>
          <div className="rounded-md border border-border bg-muted/30 px-3 py-2 text-xs space-y-1">
            <div>Facts to re-embed: <span className="font-mono">{factCount.toLocaleString()}</span></div>
            <div>
              Estimated cost:{" "}
              {spec?.local ? (
                <span className="font-mono">$0.00 (local)</span>
              ) : spec ? (
                <>
                  <span className="font-mono">~${cost.dollars.toFixed(2)}</span>{" "}
                  <span className="text-muted-foreground">
                    ({cost.tokens.toLocaleString()} tokens × ${spec.cost_per_m.toFixed(2)} / 1M)
                  </span>
                </>
              ) : (
                <span className="text-muted-foreground">unknown — verify after Test Connection</span>
              )}
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            The job runs in the background and progress streams into the existing
            ingestion activity feed. You can watch from the dashboard or the Embedding
            Model card. Resumable on failure.{" "}
            <a
              className="underline"
              href="https://github.com/Beever-AI/beever-atlas/blob/main/docs/runbooks/embedding-migration.md"
              target="_blank"
              rel="noreferrer"
            >
              Migration runbook
            </a>
          </p>
        </div>
        <div className="px-5 py-3 border-t border-border flex justify-end gap-2">
          <button
            type="button"
            className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted"
            onClick={onCancel}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="button"
            className="text-xs px-4 py-1.5 rounded-md bg-amber-600 text-white hover:bg-amber-600/90 disabled:opacity-50 font-medium inline-flex items-center gap-1.5"
            disabled={submitting}
            onClick={async () => {
              setSubmitting(true);
              try {
                await onConfirm();
              } finally {
                setSubmitting(false);
              }
            }}
          >
            {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            Start Migration
          </button>
        </div>
      </div>
    </div>
  );
}
