import { useState } from "react";
import {
  Wifi, WifiOff, Sparkles, DollarSign, Server, Scale,
  ChevronDown, Search, Layers, Image as ImageIcon, GitMerge, BookOpen, MessageCircleQuestion,
} from "lucide-react";
import { useAgentModels } from "@/hooks/useAgentModels";
import { AgentModelRow } from "./AgentModelRow";
import { AGENT_META, GROUP_LABELS } from "@/lib/agentMeta";
import type { ModelPreset } from "@/lib/types";

const PRESETS: {
  value: ModelPreset;
  label: string;
  description: string;
  Icon: React.ComponentType<{ className?: string }>;
  accent: string;
}[] = [
  { value: "balanced", label: "Balanced", description: "Recommended default mix", Icon: Scale, accent: "emerald" },
  { value: "cost_optimized", label: "Cost Optimized", description: "Cheapest models everywhere", Icon: DollarSign, accent: "amber" },
  { value: "quality_first", label: "Quality First", description: "Highest-quality responses", Icon: Sparkles, accent: "sky" },
  { value: "local_first", label: "Local First", description: "Prefer on-device models", Icon: Server, accent: "violet" },
];

const GROUP_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  ingestion: Layers,
  media: ImageIcon,
  post_processing: GitMerge,
  wiki: BookOpen,
  qa: MessageCircleQuestion,
};

const ACCENT_STYLES: Record<string, { bg: string; border: string; text: string; iconBg: string }> = {
  emerald: { bg: "bg-emerald-500/5", border: "border-emerald-500/40", text: "text-emerald-600 dark:text-emerald-400", iconBg: "bg-emerald-500/10" },
  amber: { bg: "bg-amber-500/5", border: "border-amber-500/40", text: "text-amber-600 dark:text-amber-400", iconBg: "bg-amber-500/10" },
  sky: { bg: "bg-sky-500/5", border: "border-sky-500/40", text: "text-sky-600 dark:text-sky-400", iconBg: "bg-sky-500/10" },
  violet: { bg: "bg-violet-500/5", border: "border-violet-500/40", text: "text-violet-600 dark:text-violet-400", iconBg: "bg-violet-500/10" },
};

type GroupKey = "ingestion" | "media" | "post_processing" | "wiki" | "qa";
const GROUPS: GroupKey[] = ["ingestion", "media", "post_processing", "wiki", "qa"];

export function AgentModelSettings() {
  const {
    models, defaults, availableModels, ollamaConnected,
    isLoading, error, updateModels, applyPreset,
  } = useAgentModels();

  const [pendingChanges, setPendingChanges] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [collapsed, setCollapsed] = useState<Record<GroupKey, boolean>>({
    ingestion: false, media: true, post_processing: true, wiki: true, qa: true,
  });

  const allAvailable = [
    ...(availableModels?.gemini ?? []),
    ...(availableModels?.ollama ?? []),
  ];

  const effectiveModels = { ...models, ...pendingChanges };
  const hasChanges = Object.keys(pendingChanges).length > 0;
  const customCount = Object.keys(models).filter((k) => models[k] !== defaults[k]).length;

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

  function toggleGroup(g: GroupKey) {
    setCollapsed((prev) => ({ ...prev, [g]: !prev[g] }));
  }

  if (isLoading && !Object.keys(models).length) {
    return <div className="text-sm text-muted-foreground py-8 text-center">Loading model configuration…</div>;
  }

  return (
    <div className="space-y-6">
      {/* Ollama status strip */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {customCount > 0 && (
            <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">
              {customCount} customized
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 text-xs">
          {ollamaConnected ? (
            <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20">
              <Wifi className="h-3 w-3" />
              Ollama connected ({availableModels?.ollama.length ?? 0} models)
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-muted text-muted-foreground border border-border">
              <WifiOff className="h-3 w-3" />
              Ollama not connected
            </span>
          )}
        </div>
      </div>

      {error && (
        <div className="text-xs text-destructive bg-destructive/10 rounded-md px-3 py-2">{error}</div>
      )}

      {/* Preset cards */}
      <div>
        <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-3">
          Quick Presets
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {PRESETS.map(({ value, label, description, Icon, accent }) => {
            const style = ACCENT_STYLES[accent];
            return (
              <button
                key={value}
                onClick={() => handlePreset(value)}
                disabled={saving}
                className={`group flex flex-col items-start gap-2 p-3.5 rounded-xl border-2 border-border bg-card text-left transition-all hover:${style.border} hover:${style.bg} disabled:opacity-50`}
              >
                <div className={`w-8 h-8 rounded-lg ${style.iconBg} flex items-center justify-center ${style.text}`}>
                  <Icon className="h-4 w-4" />
                </div>
                <div>
                  <div className="text-sm font-medium text-foreground">{label}</div>
                  <div className="text-xs text-muted-foreground mt-0.5">{description}</div>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {/* Search */}
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

      {/* Agent groups */}
      <div className="space-y-3">
        {GROUPS.map((group) => {
          const GroupIcon = GROUP_ICONS[group];
          const allAgents = AGENT_META.filter((a) => a.group === group);
          const agents = search
            ? allAgents.filter(
                (a) =>
                  a.displayName.toLowerCase().includes(search.toLowerCase()) ||
                  a.description.toLowerCase().includes(search.toLowerCase()),
              )
            : allAgents;
          if (agents.length === 0) return null;

          const isCollapsed = collapsed[group] && !search;
          const groupCustom = allAgents.filter((a) => effectiveModels[a.name] && effectiveModels[a.name] !== defaults[a.name]).length;
          const localCount = allAgents.filter((a) => effectiveModels[a.name]?.startsWith("ollama_chat/")).length;

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
                    <div className="text-sm font-semibold text-foreground">{GROUP_LABELS[group]}</div>
                    <div className="text-xs text-muted-foreground">
                      {agents.length} agent{agents.length !== 1 ? "s" : ""}
                      {localCount > 0 && <span> · {localCount} local</span>}
                      {groupCustom > 0 && <span> · {groupCustom} customized</span>}
                    </div>
                  </div>
                </div>
                <ChevronDown
                  className={`w-4 h-4 text-muted-foreground transition-transform ${isCollapsed ? "-rotate-90" : ""}`}
                />
              </button>
              {!isCollapsed && (
                <div className="border-t border-border p-1.5 space-y-0.5">
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
              )}
            </div>
          );
        })}
      </div>

      {/* Sticky save bar */}
      {hasChanges && (
        <div className="sticky bottom-0 -mx-5 px-5 py-3 bg-card/95 backdrop-blur border-t border-border flex items-center justify-between shadow-[0_-4px_12px_rgba(0,0,0,0.04)]">
          <span className="text-xs text-muted-foreground">
            {Object.keys(pendingChanges).length} unsaved change{Object.keys(pendingChanges).length !== 1 ? "s" : ""}
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
              className="text-xs px-4 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 font-medium"
            >
              {saving ? "Saving…" : "Save Changes"}
            </button>
          </div>
        </div>
      )}

      {toast && (
        <div className="fixed bottom-4 right-4 bg-foreground text-background text-xs px-4 py-2 rounded-md shadow-lg animate-in fade-in slide-in-from-bottom-2 z-50">
          {toast}
        </div>
      )}
    </div>
  );
}
