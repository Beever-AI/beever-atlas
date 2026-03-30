import { useState } from "react";
import { ChevronDown, ChevronRight, ExternalLink } from "lucide-react";
import type { MemoryTier2 } from "@/lib/types";

interface FactCardProps {
  fact: MemoryTier2;
}

function qualityBadgeColor(score: number): string {
  if (score >= 7) return "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300";
  if (score >= 4) return "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300";
  return "bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300";
}

function importanceBadge(importance: string): string {
  const colors: Record<string, string> = {
    critical: "bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300",
    high: "bg-orange-100 text-orange-700 dark:bg-orange-950/50 dark:text-orange-300",
    medium: "bg-blue-100 text-blue-700 dark:bg-blue-950/50 dark:text-blue-300",
    low: "bg-muted text-muted-foreground",
  };
  return colors[importance] || colors.low;
}

export function FactCard({ fact }: FactCardProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded-xl border border-border bg-background hover:bg-muted/35 transition-colors">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-start gap-2.5 p-3.5 text-left"
      >
        {expanded ? (
          <ChevronDown size={14} className="text-muted-foreground mt-0.5 shrink-0" />
        ) : (
          <ChevronRight size={14} className="text-muted-foreground mt-0.5 shrink-0" />
        )}
        <div className="flex-1 min-w-0">
          <p className="text-sm sm:text-[15px] text-foreground leading-relaxed">
            {fact.memory}
          </p>
          <div className="flex items-center gap-1.5 mt-2 flex-wrap">
            <span
              className={`px-2 py-0.5 text-xs font-semibold rounded-full ${qualityBadgeColor(fact.quality_score)}`}
            >
              quality {fact.quality_score.toFixed(1)}
            </span>
            <span
              className={`px-2 py-0.5 text-xs font-medium rounded-full capitalize ${importanceBadge(fact.importance)}`}
            >
              {fact.importance}
            </span>
            <span className="text-xs text-muted-foreground">
              {fact.user_name} &middot;{" "}
              {new Date(fact.timestamp).toLocaleDateString()}
            </span>
          </div>
        </div>
      </button>

      {expanded && (
        <div className="border-t border-border px-3.5 py-3 space-y-2">
          <div className="flex flex-wrap gap-1.5">
            {fact.entity_tags.map((tag) => (
              <span
                key={tag}
                className="px-2 py-0.5 text-xs rounded-full bg-primary/10 text-primary"
              >
                {tag}
              </span>
            ))}
            {fact.topic_tags.map((tag) => (
              <span
                key={tag}
                className="px-2 py-0.5 text-xs rounded-full bg-muted text-muted-foreground"
              >
                {tag}
              </span>
            ))}
          </div>
          <a
            href={fact.permalink}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-sm text-primary hover:text-primary/80 transition-colors"
          >
            <ExternalLink size={14} />
            View original message
          </a>
        </div>
      )}
    </div>
  );
}
