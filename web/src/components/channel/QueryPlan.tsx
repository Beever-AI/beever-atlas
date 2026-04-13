import { useState, useEffect, useRef } from "react";
import { ChevronDown, GitBranch } from "lucide-react";
import { cn } from "@/lib/utils";
import type { DecompositionPlan } from "@/types/askTypes";

interface QueryPlanProps {
  plan: DecompositionPlan;
  isStreaming: boolean;
}

export function QueryPlan({ plan, isStreaming }: QueryPlanProps) {
  const [expanded, setExpanded] = useState(true);
  const userToggledRef = useRef(false);

  const total = plan.internal.length + plan.external.length;

  useEffect(() => {
    if (!isStreaming && total > 0 && !userToggledRef.current) {
      const t = setTimeout(() => setExpanded(false), 300);
      return () => clearTimeout(t);
    }
  }, [isStreaming, total]);

  if (total === 0) return null;

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
        <GitBranch className="size-3.5" strokeWidth={2} />
        <span>Query plan ({total})</span>
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
        <div className="flex flex-col gap-1 border-l border-border pl-4 ml-1.5">
          {plan.internal.length > 0 && (
            <>
              {plan.internal.map((sq, i) => (
                <div key={i} className="flex items-start gap-2 text-xs text-muted-foreground/80">
                  <span className="mt-0.5 shrink-0 rounded px-1 py-0.5 bg-muted text-muted-foreground font-mono text-[10px] leading-none">
                    {sq.label || "internal"}
                  </span>
                  <span className="leading-relaxed">{sq.query}</span>
                </div>
              ))}
            </>
          )}
          {plan.external.length > 0 && (
            <>
              {plan.external.map((sq, i) => (
                <div key={i} className="flex items-start gap-2 text-xs text-muted-foreground/80">
                  <span className="mt-0.5 shrink-0 rounded px-1 py-0.5 bg-muted/60 text-muted-foreground font-mono text-[10px] leading-none">
                    {sq.label || "external"}
                  </span>
                  <span className="leading-relaxed">{sq.query}</span>
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
