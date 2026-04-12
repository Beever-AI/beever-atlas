import { useState, useEffect, useRef } from "react";
import { ChevronDown, Wrench } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolCallEvent } from "@/types/askTypes";
import { ToolRow } from "./ToolRow";

interface ToolListProps {
  toolCalls: ToolCallEvent[];
  isStreaming: boolean;
}

export function ToolList({ toolCalls, isStreaming }: ToolListProps) {
  const [expanded, setExpanded] = useState(true);
  const userToggledRef = useRef(false);

  const anyRunning = toolCalls.some((t) => t.status === "running");

  useEffect(() => {
    if (!isStreaming && !anyRunning && toolCalls.length > 0 && !userToggledRef.current) {
      const t = setTimeout(() => setExpanded(false), 300);
      return () => clearTimeout(t);
    }
  }, [isStreaming, anyRunning, toolCalls.length]);

  if (toolCalls.length === 0) return null;

  const toggle = () => {
    userToggledRef.current = true;
    setExpanded((v) => !v);
  };

  return (
    <div className="mb-3">
      <button
        onClick={toggle}
        className="inline-flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <Wrench className="size-3.5" strokeWidth={2} />
        <span>Tools ({toolCalls.length})</span>
        <ChevronDown
          className={cn(
            "size-3 transition-transform",
            expanded ? "rotate-0" : "-rotate-90",
          )}
        />
      </button>

      <div
        className={cn(
          "overflow-hidden transition-all duration-300 ease-in-out",
          expanded ? "opacity-100 mt-2" : "max-h-0 opacity-0",
        )}
      >
        <div className="flex flex-col gap-1">
          {toolCalls.map((tc, i) => (
            <ToolRow key={`${tc.tool_name}-${tc.started_at}-${i}`} tool={tc} />
          ))}
        </div>
      </div>
    </div>
  );
}
