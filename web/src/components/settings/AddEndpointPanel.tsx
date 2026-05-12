import { useState } from "react";
import { ExternalLink, Loader2 } from "lucide-react";
import {
  ENDPOINT_PRESETS,
  getEndpointPreset,
  type CreateEndpointRequest,
  type EndpointPreset,
} from "@/lib/aiSetup";

interface AddEndpointPanelProps {
  onCreate: (req: CreateEndpointRequest) => Promise<void>;
  onCancel: () => void;
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

/**
 * Inline expanding panel (NOT a modal) for adding an Endpoint:
 *   (1) preset chips, grouped chat / embedding-only / local;
 *   (2) form — Name, Base URL (prefilled), API key (hidden for ``none`` auth),
 *       Models (comma-separated, prefilled), + the preset's "Get an API key" link;
 *   (3) Save / Cancel — Save shows a spinner and surfaces the create error inline.
 *
 * The Add flow is Save → then Test/Discover on the resulting EndpointCard;
 * this panel does not attempt to discover pre-create.
 */
export function AddEndpointPanel({
  onCreate,
  onCancel,
  initialPresetKey,
  presetFilter,
}: AddEndpointPanelProps) {
  const available = presetFilter ? ENDPOINT_PRESETS.filter(presetFilter) : ENDPOINT_PRESETS;
  const groups = groupPresets(available);
  const firstKey = initialPresetKey && available.some((p) => p.key === initialPresetKey)
    ? initialPresetKey
    : (available[0]?.key ?? "custom");

  const [presetKey, setPresetKey] = useState(firstKey);
  const preset = getEndpointPreset(presetKey);
  const [name, setName] = useState(preset?.label ?? presetKey);
  const [baseUrl, setBaseUrl] = useState(preset?.base_url ?? "");
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<string[]>(preset?.default_models ?? []);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function selectPreset(key: string) {
    setPresetKey(key);
    const p = getEndpointPreset(key);
    setBaseUrl(p?.base_url ?? "");
    setModels(p?.default_models ?? []);
    setName(p?.label ?? key);
    setErr(null);
  }

  async function submit() {
    setSaving(true);
    setErr(null);
    try {
      const authType = preset?.auth_type ?? "api_key";
      await onCreate({
        name: name.trim() || presetKey,
        preset: presetKey,
        base_url: baseUrl,
        auth_type: authType,
        api_key: authType === "none" ? undefined : (apiKey || undefined),
        models,
      });
    } catch (e: any) {
      const detail = e?.body?.detail ?? e?.detail;
      setErr(detail?.error ? String(detail.error) : (e?.message ?? "Failed to create endpoint"));
    } finally {
      setSaving(false);
    }
  }

  const showApiKey = preset?.auth_type !== "none";

  return (
    <div className="rounded-xl border border-border bg-muted/30 p-4 space-y-4">
      {/* (1) Preset chips */}
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
            <input
              type="password"
              className="w-full text-sm font-mono rounded-md border border-border bg-background px-2.5 py-1.5"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
            />
          </div>
        )}
        <div className="space-y-1">
          <label className="text-sm font-medium text-foreground">Models</label>
          <input
            className="w-full text-sm font-mono rounded-md border border-border bg-background px-2.5 py-1.5"
            value={models.join(", ")}
            onChange={(e) => setModels(e.target.value.split(",").map((m) => m.trim()).filter(Boolean))}
            placeholder="comma-separated model names"
          />
        </div>
        {preset?.docs_url && (
          <a
            href={preset.docs_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
          >
            Get an API key <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </div>

      {err && <div className="text-xs text-destructive">{err}</div>}

      {/* (3) Save / Cancel */}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={submit}
          disabled={saving}
          className="inline-flex items-center gap-1.5 text-sm rounded-md bg-primary text-primary-foreground px-3.5 py-1.5 hover:bg-primary/90 disabled:opacity-50 font-medium"
        >
          {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />} Save
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="text-sm rounded-md border border-border px-3.5 py-1.5 hover:bg-muted"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
