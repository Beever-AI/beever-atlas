import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, ExternalLink, KeyRound, Loader2, Plus, X } from "lucide-react";
import {
  ENDPOINT_PRESETS,
  getEndpointPreset,
  modelsDocsUrl,
  type CreateEndpointRequest,
  type Endpoint,
  type EndpointPreset,
  type EndpointRole,
  type UpdateEndpointRequest,
} from "@/lib/aiSetup";

/**
 * PR-β: presets where the role radio is offered. Embedding-only presets
 * (``jina_ai`` / ``voyage`` / ``cohere``) hide the radio and seed
 * ``role="embedding"``; chat-only providers hide the radio and seed
 * ``role="chat"``; everything else (ambiguous) renders the radio with
 * ``role="both"`` as the default.
 */
const AMBIGUOUS_ROLE_PRESETS = new Set<string>([
  "openai",
  "google_ai",
  "ollama",
  "custom",
  "litellm_proxy",
  "openrouter",
  "vllm",
  "lmstudio",
  "bedrock",
  "vertex_ai",
]);
const EMBEDDING_ONLY_PRESET_ROLES = new Set<string>(["jina_ai", "voyage", "cohere"]);
const CHAT_ONLY_PRESET_ROLES = new Set<string>([
  "anthropic",
  "mistral",
  "deepseek",
  "groq",
  "xai",
  "minimax",
  "together_ai",
]);

function defaultRoleForPreset(presetKey: string): EndpointRole {
  if (EMBEDDING_ONLY_PRESET_ROLES.has(presetKey)) return "embedding";
  if (CHAT_ONLY_PRESET_ROLES.has(presetKey)) return "chat";
  return "both";
}

/** Mode the panel renders in — see ``AddEndpointPanelProps``. */
export type EndpointPanelMode = "create" | "edit";

interface AddEndpointPanelProps {
  /** Create handler — used in ``"create"`` mode. */
  onCreate?: (req: CreateEndpointRequest) => Promise<void>;
  /**
   * Update handler — used in ``"edit"`` mode. ``api_key`` is omitted from the
   * request unless the user explicitly replaced it, so the backend keeps the
   * stored credential.
   */
  onUpdate?: (req: UpdateEndpointRequest) => Promise<void>;
  onCancel: () => void;
  /** ``"create"`` (default) shows the preset chips; ``"edit"`` locks the preset. */
  mode?: EndpointPanelMode;
  /** The endpoint being edited — required in ``"edit"`` mode, ignored otherwise. */
  existing?: Endpoint;
  /** Preset to start on; defaults to the first preset passing the filter. */
  initialPresetKey?: string;
  /** Restrict which presets are offered (e.g. embedding-capable only). */
  presetFilter?: (p: EndpointPreset) => boolean;
}

interface PresetGroup {
  label: string;
  presets: EndpointPreset[];
}

function groupPresets(presets: EndpointPreset[]): PresetGroup[] {
  const local = presets.filter((p) => p.local);
  const embeddingOnly = presets.filter((p) => !p.local && p.embedding_only);
  const chat = presets.filter((p) => !p.local && !p.embedding_only);
  return [
    { label: "Chat providers", presets: chat },
    { label: "Embedding-only", presets: embeddingOnly },
    { label: "Local", presets: local },
  ].filter((g) => g.presets.length > 0);
}

interface HeaderRow {
  k: string;
  v: string;
}

function headersToRows(headers: Record<string, string>): HeaderRow[] {
  return Object.entries(headers).map(([k, v]) => ({ k, v }));
}

function rowsToHeaders(rows: HeaderRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const { k, v } of rows) {
    const key = k.trim();
    if (key) out[key] = v;
  }
  return out;
}

/**
 * Modal dialog for adding *or editing* an Endpoint. Renders as a centered
 * ``bg-card`` panel over a ``bg-black/40 backdrop-blur-sm`` backdrop (mirrors
 * the platform-picker dialog in ``SettingsPage``); closes on backdrop click,
 * the ✕ button, or Escape (all routed through ``onCancel``).
 *   - **create** mode: (1) preset chips, grouped chat / embedding-only / local;
 *     (2) form — Name, Base URL, API key (hidden for ``none`` auth), Models;
 *     (3) Advanced — RPM, extra headers, tags; (4) Save / Cancel.
 *   - **edit** mode: the preset is fixed (rendered as a read-only label); the
 *     API key shows a masked placeholder with a "Replace key" link that reveals
 *     an empty password input — the request only carries ``api_key`` if the user
 *     actually changed it, so the backend's "PUT preserves the unchanged
 *     credential" behaviour applies. Same Advanced section.
 *
 * The Add flow is Save → then Test/Discover on the resulting EndpointCard.
 */
export function AddEndpointPanel({
  onCreate,
  onUpdate,
  onCancel,
  mode = "create",
  existing,
  initialPresetKey,
  presetFilter,
}: AddEndpointPanelProps) {
  const isEdit = mode === "edit" && !!existing;

  const available = presetFilter ? ENDPOINT_PRESETS.filter(presetFilter) : ENDPOINT_PRESETS;
  const groups = groupPresets(available);
  const createFirstKey = initialPresetKey && available.some((p) => p.key === initialPresetKey)
    ? initialPresetKey
    : (available[0]?.key ?? "custom");

  // In edit mode the preset is whatever the endpoint already has.
  const initialPreset = isEdit ? existing!.preset : createFirstKey;
  const [presetKey, setPresetKey] = useState(initialPreset);
  const preset = getEndpointPreset(presetKey);
  const presetLabel = preset?.label ?? presetKey;

  const [name, setName] = useState(isEdit ? existing!.name : (preset?.label ?? presetKey));
  const [baseUrl, setBaseUrl] = useState(isEdit ? existing!.base_url : (preset?.base_url ?? ""));
  const [apiKey, setApiKey] = useState("");
  // In edit mode the field starts hidden behind a "Replace key" link.
  const [revealKeyInput, setRevealKeyInput] = useState(!isEdit);
  const [models, setModels] = useState<string[]>(isEdit ? existing!.models : (preset?.default_models ?? []));
  const [modelInput, setModelInput] = useState("");

  // Advanced section — collapsed by default in both modes.
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const presetRpmDefault = 60;
  const [rpm, setRpm] = useState<string>(String(isEdit ? existing!.rpm : presetRpmDefault));
  const [headerRows, setHeaderRows] = useState<HeaderRow[]>(isEdit ? headersToRows(existing!.headers) : []);
  const [tags, setTags] = useState<string[]>(isEdit ? existing!.tags : []);
  const [tagsRaw, setTagsRaw] = useState((isEdit ? existing!.tags : []).join(", "));

  // PR-β: soft role for the Test probe + model picker. Edit mode prefills
  // from the existing endpoint; create mode derives from the preset.
  const initialRole: EndpointRole = isEdit
    ? (existing!.role ?? defaultRoleForPreset(existing!.preset))
    : defaultRoleForPreset(initialPreset);
  const [role, setRole] = useState<EndpointRole>(initialRole);

  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Close on Escape — matches the rest of the Settings modals.
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "Escape") onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  function selectPreset(key: string) {
    setPresetKey(key);
    const p = getEndpointPreset(key);
    setBaseUrl(p?.base_url ?? "");
    setModels(p?.default_models ?? []);
    setName(p?.label ?? key);
    // PR-β: reset role to the new preset's natural default.
    setRole(defaultRoleForPreset(key));
    setErr(null);
  }

  function addModel() {
    const m = modelInput.trim();
    setModelInput("");
    if (!m || models.includes(m)) return;
    setModels([...models, m]);
  }
  function removeModel(m: string) {
    setModels(models.filter((x) => x !== m));
  }

  function commitTags(raw: string) {
    setTagsRaw(raw);
    setTags(raw.split(",").map((t) => t.trim()).filter(Boolean));
  }

  function updateHeaderRow(i: number, patch: Partial<HeaderRow>) {
    setHeaderRows((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }
  function addHeaderRow() {
    setHeaderRows((rows) => [...rows, { k: "", v: "" }]);
  }
  function removeHeaderRow(i: number) {
    setHeaderRows((rows) => rows.filter((_, idx) => idx !== i));
  }

  async function submit() {
    if (isEdit ? !onUpdate : !onCreate) return;
    setSaving(true);
    setErr(null);
    try {
      const authType = preset?.auth_type ?? "api_key";
      const parsedRpm = Number.parseInt(rpm, 10);
      const headers = rowsToHeaders(headerRows);
      const common = {
        name: name.trim() || presetKey,
        base_url: baseUrl,
        models,
        ...(Number.isFinite(parsedRpm) && parsedRpm > 0 ? { rpm: parsedRpm } : {}),
        ...(Object.keys(headers).length > 0 || (isEdit && Object.keys(existing!.headers).length > 0) ? { headers } : {}),
        ...(tags.length > 0 || (isEdit && existing!.tags.length > 0) ? { tags } : {}),
      };
      if (isEdit) {
        // ``api_key`` is only sent when the user opened the input *and* typed
        // something — otherwise the backend keeps the stored credential.
        const apiKeyChanged = revealKeyInput && apiKey.trim().length > 0;
        const req: UpdateEndpointRequest = {
          ...common,
          auth_type: authType,
          ...(authType === "none" ? {} : apiKeyChanged ? { api_key: apiKey } : {}),
          // PR-β: only send role on edit when it actually changed from the
          // persisted value — keeps the wire payload minimal.
          ...(role !== (existing!.role ?? defaultRoleForPreset(existing!.preset))
            ? { role }
            : {}),
        };
        await onUpdate!(req);
      } else {
        const req: CreateEndpointRequest = {
          ...common,
          preset: presetKey,
          auth_type: authType,
          api_key: authType === "none" ? undefined : (apiKey || undefined),
          // PR-β: always send the role so the backend persists exactly what
          // the operator picked (vs. its preset-default).
          role,
        };
        await onCreate!(req);
      }
    } catch (e: any) {
      const detail = e?.body?.detail ?? e?.detail;
      setErr(
        detail?.error
          ? String(detail.error)
          : (e?.message ?? (isEdit ? "Failed to update endpoint" : "Failed to create endpoint")),
      );
    } finally {
      setSaving(false);
    }
  }

  const showApiKey = (preset?.auth_type ?? "api_key") !== "none";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-sm"
        onClick={onCancel}
        aria-hidden="true"
      />

      {/* Dialog */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={isEdit ? "Edit endpoint" : "Add endpoint"}
        className="relative z-10 w-full max-w-2xl bg-card border border-border rounded-2xl shadow-2xl flex flex-col max-h-[88vh] overflow-hidden"
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-3 px-6 py-4 border-b border-border shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            <h3 className="text-base font-semibold text-foreground truncate">
              {isEdit ? "Edit endpoint" : "Add endpoint"}
            </h3>
            {isEdit && (
              <span className="inline-flex items-center rounded-md bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground shrink-0">
                {presetLabel}
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={onCancel}
            className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-muted transition-colors shrink-0"
            aria-label="Close"
          >
            <X className="w-4 h-4 text-muted-foreground" />
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 space-y-4 flex-1 min-h-0 overflow-y-auto">

      {/* (1) Preset chips — create mode only */}
      {!isEdit && (
        <div className="space-y-2.5">
          {groups.map((g) => (
            <div key={g.label} className="space-y-1.5">
              <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{g.label}</div>
              <div className="flex flex-wrap gap-1.5">
                {g.presets.map((p) => (
                  <button
                    key={p.key}
                    type="button"
                    onClick={() => selectPreset(p.key)}
                    className={`rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
                      p.key === presetKey
                        ? "border-primary bg-primary/10 text-foreground"
                        : "border-border hover:bg-muted"
                    }`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* PR-β: Role radio — shown only for ambiguous presets. Embedding-only
          and chat-only presets seed the role silently. */}
      {AMBIGUOUS_ROLE_PRESETS.has(presetKey) && (
        <div className="space-y-1.5" role="radiogroup" aria-label="endpoint role">
          <div className="text-sm font-medium text-foreground">
            What will you use this endpoint for?
          </div>
          <div className="flex flex-wrap gap-1.5">
            {([
              { value: "both", label: "Both (default)" },
              { value: "chat", label: "Chat agents" },
              { value: "embedding", label: "Embeddings" },
            ] as { value: EndpointRole; label: string }[]).map((opt) => (
              <label
                key={opt.value}
                className={`inline-flex items-center gap-1.5 cursor-pointer rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
                  role === opt.value
                    ? "border-primary bg-primary/10 text-foreground"
                    : "border-border hover:bg-muted"
                }`}
              >
                <input
                  type="radio"
                  name="endpoint-role"
                  value={opt.value}
                  checked={role === opt.value}
                  onChange={() => setRole(opt.value)}
                  className="sr-only"
                  aria-label={opt.label}
                />
                {opt.label}
              </label>
            ))}
          </div>
        </div>
      )}

      {/* (2) Form */}
      <div className="space-y-3">
        <div className="space-y-1">
          <label className="text-sm font-medium text-foreground">Name</label>
          <input
            className="w-full text-sm rounded-md border border-border bg-background px-2.5 py-1.5"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={preset?.label ?? presetKey}
          />
        </div>
        <div className="space-y-1">
          <label className="text-sm font-medium text-foreground">Base URL</label>
          <input
            className="w-full text-sm font-mono rounded-md border border-border bg-background px-2.5 py-1.5"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="https://api.example.com/v1"
          />
        </div>
        {showApiKey && (
          <div className="space-y-1">
            <label className="text-sm font-medium text-foreground">API key</label>
            {isEdit && !revealKeyInput ? (
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-mono text-muted-foreground truncate">
                  {existing!.has_credential ? existing!.credential_masked : "no credential stored"}
                </span>
                <button
                  type="button"
                  onClick={() => {
                    setRevealKeyInput(true);
                    setApiKey("");
                  }}
                  className="text-xs text-primary hover:underline"
                >
                  Replace key
                </button>
              </div>
            ) : (
              <>
                <input
                  type="password"
                  className="w-full text-sm font-mono rounded-md border border-border bg-background px-2.5 py-1.5"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={isEdit ? "enter a new key…" : "sk-..."}
                  autoFocus={isEdit}
                />
                {isEdit && (
                  <button
                    type="button"
                    onClick={() => {
                      setRevealKeyInput(false);
                      setApiKey("");
                    }}
                    className="text-xs text-muted-foreground hover:text-foreground"
                  >
                    Keep existing key
                  </button>
                )}
              </>
            )}
          </div>
        )}
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <label className="text-sm font-medium text-foreground">Models</label>
            <a
              href={modelsDocsUrl(presetKey)}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
              title={`Open ${presetLabel} model catalog in a new tab`}
            >
              View available models
              <ExternalLink className="h-3 w-3" />
            </a>
          </div>
          <p className="text-xs text-muted-foreground">
            The models this endpoint serves — pickable on the Embedding &amp; Agent-models tabs.
            Add them by name, or hit <span className="font-medium">Discover</span> on the endpoint card after saving.
          </p>
          {models.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {models.map((m) => (
                <span
                  key={m}
                  className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs font-mono text-foreground"
                >
                  {m}
                  <button
                    type="button"
                    onClick={() => removeModel(m)}
                    className="text-muted-foreground hover:text-destructive"
                    aria-label={`remove model ${m}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="flex items-center gap-1.5">
            <input
              className="flex-1 min-w-0 text-sm font-mono rounded-md border border-border bg-background px-2.5 py-1.5"
              value={modelInput}
              onChange={(e) => setModelInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addModel();
                }
              }}
              placeholder="add a model…"
              aria-label="add a model"
            />
            <button
              type="button"
              onClick={addModel}
              disabled={!modelInput.trim()}
              className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
            >
              <Plus className="h-3.5 w-3.5" /> Add
            </button>
          </div>
        </div>
        {preset?.docs_url && (
          <a
            href={preset.docs_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs font-medium text-primary hover:bg-primary/5 hover:border-primary/40 transition-colors"
          >
            <KeyRound className="h-3.5 w-3.5" />
            Get an API key for {presetLabel}
            <ExternalLink className="h-3 w-3 opacity-70" />
          </a>
        )}
      </div>

      {/* Advanced — collapsed by default */}
      <div className="rounded-lg border border-border bg-background/50">
        <button
          type="button"
          onClick={() => setAdvancedOpen((v) => !v)}
          className="flex w-full items-center gap-1.5 px-3 py-2 text-xs font-medium text-muted-foreground hover:text-foreground"
          aria-expanded={advancedOpen}
        >
          {advancedOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          Advanced
        </button>
        {advancedOpen && (
          <div className="space-y-3 border-t border-border px-3 py-3">
            <div className="space-y-1">
              <label className="text-xs font-medium text-foreground">Rate limit (RPM)</label>
              <input
                type="number"
                min={1}
                className="w-32 text-sm rounded-md border border-border bg-background px-2.5 py-1.5"
                value={rpm}
                onChange={(e) => setRpm(e.target.value)}
                placeholder="60"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-foreground">Extra headers</label>
              {headerRows.length === 0 && (
                <p className="text-[11px] text-muted-foreground">No custom headers.</p>
              )}
              {headerRows.map((row, i) => (
                <div key={i} className="flex items-center gap-1.5">
                  <input
                    className="flex-1 min-w-0 text-xs font-mono rounded-md border border-border bg-background px-2 py-1"
                    value={row.k}
                    onChange={(e) => updateHeaderRow(i, { k: e.target.value })}
                    placeholder="Header-Name"
                    aria-label={`header name ${i + 1}`}
                  />
                  <input
                    className="flex-1 min-w-0 text-xs font-mono rounded-md border border-border bg-background px-2 py-1"
                    value={row.v}
                    onChange={(e) => updateHeaderRow(i, { v: e.target.value })}
                    placeholder="value"
                    aria-label={`header value ${i + 1}`}
                  />
                  <button
                    type="button"
                    onClick={() => removeHeaderRow(i)}
                    className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-destructive"
                    aria-label={`remove header ${i + 1}`}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={addHeaderRow}
                className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
              >
                <Plus className="h-3 w-3" /> Add header
              </button>
            </div>
            <div className="space-y-1">
              <label className="text-xs font-medium text-foreground">Tags</label>
              <input
                className="w-full text-xs rounded-md border border-border bg-background px-2.5 py-1.5"
                value={tagsRaw}
                onChange={(e) => commitTags(e.target.value)}
                placeholder="comma-separated tags"
              />
            </div>
          </div>
        )}
      </div>

          {err && <div className="text-xs text-destructive">{err}</div>}
        </div>

        {/* Footer — Save / Cancel */}
        <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-border bg-muted/30 shrink-0">
          <button
            type="button"
            onClick={onCancel}
            className="text-sm rounded-md border border-border px-3.5 py-1.5 hover:bg-muted"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={saving}
            className="inline-flex items-center gap-1.5 text-sm rounded-md bg-primary text-primary-foreground px-3.5 py-1.5 hover:bg-primary/90 disabled:opacity-50 font-medium"
          >
            {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />} {isEdit ? "Save changes" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
