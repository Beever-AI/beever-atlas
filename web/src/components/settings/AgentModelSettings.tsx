import { useState } from "react";
import { Cpu, Wifi, WifiOff, Sparkles, DollarSign, Server, Scale } from "lucide-react";
import { useAgentModels } from "@/hooks/useAgentModels";
import { AgentModelRow } from "./AgentModelRow";
import { AGENT_META, GROUP_LABELS } from "@/lib/agentMeta";
import type { ModelPreset } from "@/lib/types";

const PRESETS: { value: ModelPreset; label: string; Icon: React.ComponentType<{ className?: string }> }[] = [
  { value: "balanced", label: "Balanced", Icon: Scale },
  { value: "cost_optimized", label: "Cost Optimized", Icon: DollarSign },
  { value: "quality_first", label: "Quality First", Icon: Sparkles },
  { value: "local_first", label: "Local First", Icon: Server },
];

export function AgentModelSettings() {
  const {
    models, defaults, availableModels, ollamaConnected,
    isLoading, error, updateModels, applyPreset,
  } = useAgentModels();

  const [pendingChanges, setPendingChanges] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const allAvailable = [
    ...(availableModels?.gemini ?? []),
    ...(availableModels?.ollama ?? []),
  ];

  const effectiveModels = { ...models, ...pendingChanges };
  const hasChanges = Object.keys(pendingChanges).length > 0;

  function handleChange(agentName: string, model: string) {
    setPendingChanges((prev) => {
      if (model === models[agentName]) {
        const next = { ...prev };
        delete next[agentName];
        return next;
      }
      return { ...prev, [agentName]: model };
    });
  }

  async function handleSave() {
    if (!hasChanges) return;
    setSaving(true);
    try {
      await updateModels(pendingChanges);
      setPendingChanges({});
      showToast("Model configuration saved");
    } catch {
      showToast("Failed to save");
    } finally {
      setSaving(false);
    }
  }

  async function handlePreset(preset: ModelPreset) {
    setSaving(true);
    try {
      await applyPreset(preset);
      setPendingChanges({});
      showToast(`Applied "${preset.replace("_", " ")}" preset`);
    } catch {
      showToast("Failed to apply preset");
    } finally {
      setSaving(false);
    }
  }

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 2500);
  }

  if (isLoading && !Object.keys(models).length) {
    return <div className="text-sm text-muted-foreground py-4">Loading model configuration...</div>;
  }

  const groups = ["ingestion", "media", "post_processing", "wiki"] as const;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Cpu className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-semibold text-foreground">AI Models</h3>
        </div>
        <div className="flex items-center gap-2 text-xs">
          {ollamaConnected ? (
            <span className="flex items-center gap-1 text-emerald-500">
              <Wifi className="h-3 w-3" />
              Ollama connected ({availableModels?.ollama.length ?? 0} models)
            </span>
          ) : (
            <span className="flex items-center gap-1 text-muted-foreground">
              <WifiOff className="h-3 w-3" />
              Ollama not connected
            </span>
          )}
        </div>
      </div>

      {error && (
        <div className="text-xs text-destructive bg-destructive/10 rounded-md px-3 py-2">{error}</div>
      )}

      {/* Presets */}
      <div className="flex gap-2">
        {PRESETS.map(({ value, label, Icon }) => (
          <button
            key={value}
            onClick={() => handlePreset(value)}
            disabled={saving}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md border border-border bg-background hover:bg-muted transition-colors disabled:opacity-50"
          >
            <Icon className="h-3 w-3" />
            {label}
          </button>
        ))}
      </div>

      {/* Agent groups */}
      {groups.map((group) => {
        const agents = AGENT_META.filter((a) => a.group === group);
        return (
          <div key={group} className="space-y-1">
            <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide pt-2">
              {GROUP_LABELS[group]}
            </div>
            <div className="divide-y divide-border">
              {agents.map((agent) => (
                <AgentModelRow
                  key={agent.name}
                  agent={agent}
                  currentModel={effectiveModels[agent.name] ?? ""}
                  defaultModel={defaults[agent.name] ?? ""}
                  availableModels={allAvailable}
                  onChange={handleChange}
                />
              ))}
            </div>
          </div>
        );
      })}

      {/* Save bar */}
      {hasChanges && (
        <div className="flex items-center justify-between pt-2 border-t border-border">
          <span className="text-xs text-muted-foreground">
            {Object.keys(pendingChanges).length} unsaved change(s)
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setPendingChanges({})}
              className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted"
            >
              Discard
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="text-xs px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {saving ? "Saving..." : "Save Changes"}
            </button>
          </div>
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div className="fixed bottom-4 right-4 bg-foreground text-background text-xs px-4 py-2 rounded-md shadow-lg animate-in fade-in slide-in-from-bottom-2">
          {toast}
        </div>
      )}
    </div>
  );
}
