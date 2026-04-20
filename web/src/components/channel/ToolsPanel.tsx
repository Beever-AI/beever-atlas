import { useState } from "react";
import { ChevronDown } from "lucide-react";
import type { ToolDescriptor, ToolCategory } from "@/types/toolTypes";

interface ToolsPanelProps {
  descriptors: ToolDescriptor[];
  disabledTools: string[];
  onToggle: (name: string) => void;
  className?: string;
}

const CATEGORY_LABELS: Record<ToolCategory, string> = {
  wiki: "Wiki",
  memory: "Memory",
  graph: "Graph",
  external: "External",
  orchestration: "Orchestration",
};

const CATEGORY_ORDER: ToolCategory[] = ["wiki", "memory", "graph", "external", "orchestration"];

export function ToolsPanel({
  descriptors,
  disabledTools,
  onToggle,
  className = "",
}: ToolsPanelProps) {
  const [open, setOpen] = useState(false);

  const total = descriptors.length;
  const enabled = descriptors.filter((d) => !disabledTools.includes(d.name)).length;

  // Group descriptors by category, preserving CATEGORY_ORDER
  const grouped = CATEGORY_ORDER.reduce<Record<ToolCategory, ToolDescriptor[]>>(
    (acc, cat) => {
      acc[cat] = descriptors.filter((d) => d.category === cat);
      return acc;
    },
    { wiki: [], memory: [], graph: [], external: [], orchestration: [] },
  );

  return (
    <div className={`border border-border rounded-xl bg-card overflow-hidden ${className}`}>
      {/* Header / toggle row */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-4 py-2.5 text-left hover:bg-muted/40 transition-colors"
        aria-expanded={open}
      >
        <span className="text-[13px] font-medium text-foreground/80">
          Tools ({enabled}/{total} enabled)
        </span>
        <ChevronDown
          className={`w-4 h-4 text-muted-foreground transition-transform duration-200 ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>

      {/* Expandable body */}
      {open && (
        <div className="border-t border-border divide-y divide-border/50">
          {CATEGORY_ORDER.map((cat) => {
            const tools = grouped[cat];
            if (tools.length === 0) return null;
            return (
              <div key={cat} className="px-4 py-2">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/60 mb-1.5">
                  {CATEGORY_LABELS[cat]}
                </div>
                <div className="space-y-1">
                  {tools.map((tool) => {
                    const isDisabled = disabledTools.includes(tool.name);
                    return (
                      <div
                        key={tool.name}
                        className="flex items-center justify-between gap-3 py-1"
                      >
                        <div className="min-w-0 flex-1">
                          <span className="block text-[13px] font-semibold text-foreground truncate">
                            {tool.name}
                          </span>
                          <span className="block text-[12px] text-muted-foreground leading-snug">
                            {tool.description}
                          </span>
                        </div>
                        <button
                          type="button"
                          role="switch"
                          aria-checked={!isDisabled}
                          aria-pressed={!isDisabled}
                          aria-label={`${isDisabled ? "Enable" : "Disable"} ${tool.name}`}
                          onClick={() => onToggle(tool.name)}
                          className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 ${
                            isDisabled
                              ? "bg-muted"
                              : "bg-primary"
                          }`}
                        >
                          <span className="sr-only">
                            {isDisabled ? "Enable" : "Disable"} {tool.name}
                          </span>
                          <span
                            className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow-sm ring-0 transition-transform duration-200 ${
                              isDisabled ? "translate-x-0" : "translate-x-4"
                            }`}
                          />
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
