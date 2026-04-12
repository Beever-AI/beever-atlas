import { useState } from "react";
import {
  BookOpen,
  ChevronDown,
  Globe,
  Network,
  Search,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolCallEvent } from "@/types/askTypes";
import { getToolLabel, TOOL_CATEGORIES, type ToolCategory } from "@/constants/toolLabels";

const CATEGORY_GLYPHS: Record<ToolCategory, LucideIcon> = {
  wiki: BookOpen,
  search: Search,
  graph: Network,
  external: Globe,
};

interface ToolRowProps {
  tool: ToolCallEvent;
}

export function ToolRow({ tool }: ToolRowProps) {
  const [expanded, setExpanded] = useState(false);

  const category = TOOL_CATEGORIES[tool.tool_name];
  const Icon: LucideIcon = category ? CATEGORY_GLYPHS[category] : Wrench;
  const label = getToolLabel(tool.tool_name);
  const isRunning = tool.status === "running";
  const isError = tool.status === "error";

  return (
    <div>
      <button
        onClick={() => !isRunning && setExpanded((v) => !v)}
        disabled={isRunning}
        className={cn(
          "flex items-center gap-2 w-full text-left h-9 px-2 rounded-md",
          "text-sm text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors",
          "disabled:cursor-default disabled:hover:bg-transparent",
        )}
      >
        <Icon
          className={cn(
            "size-3.5 shrink-0",
            isError ? "text-destructive" : "text-muted-foreground",
          )}
          strokeWidth={2}
        />
        <span className={cn("truncate", !isRunning && "text-foreground/90")}>
          {label}
        </span>

        <span className="ml-auto inline-flex items-center gap-2 text-xs text-muted-foreground/70">
          {isRunning && (
            <>
              <span className="inline-block size-1.5 rounded-full bg-primary animate-pulse" />
              <span>Running</span>
            </>
          )}
          {!isRunning && (
            <ChevronDown
              className={cn(
                "size-3 transition-transform",
                expanded ? "rotate-0" : "-rotate-90",
              )}
            />
          )}
        </span>
      </button>

      {expanded && !isRunning && (
        <div className="mt-1 ml-7 mr-2 mb-2 rounded-md bg-muted/30 border border-border/50 p-3 space-y-2 text-xs">
          {tool.input && Object.keys(tool.input).length > 0 && (
            <div>
              <div className="text-muted-foreground/60 mb-1">Input</div>
              <pre className="font-mono text-muted-foreground/80 whitespace-pre-wrap break-all">
                {JSON.stringify(tool.input, null, 0).slice(0, 400)}
              </pre>
            </div>
          )}
          {tool.result_summary && (
            <div>
              <div className="text-muted-foreground/60 mb-1">Output</div>
              <div className="text-muted-foreground/90">{tool.result_summary}</div>
            </div>
          )}
          {(tool.latency_ms != null || (tool.facts_found ?? 0) > 0) && (
            <div className="text-muted-foreground/60">
              {tool.latency_ms != null && <>Latency · {tool.latency_ms}ms</>}
              {(tool.facts_found ?? 0) > 0 && <> · {tool.facts_found} found</>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
