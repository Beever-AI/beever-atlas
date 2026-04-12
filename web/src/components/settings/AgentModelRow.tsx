import { RotateCcw, Cloud, HardDrive } from "lucide-react";
import type { AgentMeta } from "@/lib/agentMeta";

interface Props {
  agent: AgentMeta;
  currentModel: string;
  defaultModel: string;
  availableModels: string[];
  onChange: (agentName: string, model: string) => void;
}

function ProviderBadge({ model }: { model: string }) {
  const isOllama = model.startsWith("ollama_chat/");
  if (isOllama) {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded bg-violet-500/10 text-violet-600 dark:text-violet-400 border border-violet-500/20">
        <HardDrive className="w-2.5 h-2.5" />
        Local
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded bg-sky-500/10 text-sky-600 dark:text-sky-400 border border-sky-500/20">
      <Cloud className="w-2.5 h-2.5" />
      Cloud
    </span>
  );
}

export function AgentModelRow({ agent, currentModel, defaultModel, availableModels, onChange }: Props) {
  const isDefault = currentModel === defaultModel;

  return (
    <div className="group flex items-center gap-4 py-2.5 px-3 rounded-lg hover:bg-muted/30 transition-colors">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-foreground truncate">{agent.displayName}</span>
          {!isDefault && (
            <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">
              Custom
            </span>
          )}
        </div>
        <div className="text-xs text-muted-foreground truncate">{agent.description}</div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <ProviderBadge model={currentModel} />
        <select
          value={currentModel}
          onChange={(e) => onChange(agent.name, e.target.value)}
          className="text-xs bg-background border border-border rounded-md px-2 py-1.5 min-w-[200px] text-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 hover:border-primary/40 transition-colors"
        >
          <optgroup label="Gemini (Cloud)">
            {availableModels
              .filter((m) => m.startsWith("gemini-"))
              .map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
          </optgroup>
          <optgroup label="Local (Ollama)">
            {availableModels
              .filter((m) => m.startsWith("ollama_chat/"))
              .map((m) => (
                <option key={m} value={m}>{m.replace("ollama_chat/", "")}</option>
              ))}
          </optgroup>
        </select>
        <button
          onClick={() => onChange(agent.name, defaultModel)}
          disabled={isDefault}
          className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted disabled:opacity-0 disabled:pointer-events-none transition-all"
          title="Reset to default"
        >
          <RotateCcw className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  );
}
