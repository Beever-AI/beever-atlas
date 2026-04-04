import { useEffect, useState } from "react";
import type { HealthResponse } from "@/lib/types";
import { api } from "@/lib/api";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

interface HealthBadgeProps {
  collapsed?: boolean;
}

type BadgeStatus = "healthy" | "degraded" | "unhealthy" | "loading";

const statusConfig: Record<BadgeStatus, { dot: string; label: string; tooltip: string }> = {
  healthy:   { dot: "bg-emerald-500", label: "Operational",   tooltip: "All systems operational" },
  degraded:  { dot: "bg-amber-500",   label: "Degraded",      tooltip: "Some components degraded" },
  unhealthy: { dot: "bg-rose-500",    label: "Down",          tooltip: "Systems unavailable" },
  loading:   { dot: "bg-muted-foreground/40 animate-pulse", label: "Connecting", tooltip: "Checking system health..." },
};

export function HealthBadge({ collapsed = false }: HealthBadgeProps) {
  const [status, setStatus] = useState<BadgeStatus>("loading");
  const [components, setComponents] = useState<Record<string, string>>({});

  useEffect(() => {
    let mounted = true;

    async function checkHealth() {
      try {
        const data = await api.get<HealthResponse>("/api/health");
        if (!mounted) return;
        setStatus(data.status);
        const map: Record<string, string> = {};
        for (const [name, c] of Object.entries(data.components)) {
          map[name] = c.status;
        }
        setComponents(map);
      } catch {
        if (mounted) {
          setStatus("loading");
          setComponents({});
        }
      }
    }

    checkHealth();
    const interval = setInterval(checkHealth, 30_000);
    return () => { mounted = false; clearInterval(interval); };
  }, []);

  const config = statusConfig[status];

  const tooltipContent = (
    <div className="space-y-1 text-xs">
      <p className="font-medium">{config.tooltip}</p>
      {Object.entries(components).map(([name, s]) => (
        <div key={name} className="flex items-center gap-1.5">
          <span className={cn(
            "w-1.5 h-1.5 rounded-full shrink-0",
            s === "up" ? "bg-emerald-400" : "bg-rose-400"
          )} />
          <span className="capitalize">{name}</span>
          <span className={cn(
            "ml-auto",
            s === "up" ? "text-emerald-400 dark:text-emerald-300" : "text-rose-400 dark:text-rose-300"
          )}>
            {s}
          </span>
        </div>
      ))}
    </div>
  );

  if (collapsed) {
    return (
      <Tooltip>
        <TooltipTrigger
          render={
            <button className="flex items-center justify-center" aria-label={config.label}>
              <span className={cn("w-2 h-2 rounded-full", config.dot)} />
            </button>
          }
        />
        <TooltipContent side="right">{tooltipContent}</TooltipContent>
      </Tooltip>
    );
  }

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button className="flex items-center gap-2 w-full text-left">
            <span className={cn("w-2 h-2 rounded-full shrink-0", config.dot)} />
            <span className="text-xs text-muted-foreground truncate">{config.label}</span>
          </button>
        }
      />
      <TooltipContent side="top" align="start">{tooltipContent}</TooltipContent>
    </Tooltip>
  );
}
