import { useState } from "react";
import { ChevronDown, ChevronRight, ExternalLink, ImageIcon, FileText, Film } from "lucide-react";
import type { MemoryTier2 } from "@/lib/types";

interface FactCardProps {
  fact: MemoryTier2;
}

function qualityBadgeColor(score: number): string {
  if (score >= 0.7) return "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300";
  if (score >= 0.4) return "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300";
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

function formatTimestamp(ts: string | null): string {
  if (!ts) return "";
  try {
    // Handle Slack epoch timestamps (e.g., "1711670772.571000")
    if (/^\d+\.\d+$/.test(ts)) {
      return new Date(parseFloat(ts) * 1000).toLocaleDateString();
    }
    return new Date(ts).toLocaleDateString();
  } catch {
    return "";
  }
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
            {fact.memory_text}
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
            {fact.source_media_type && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full bg-violet-100 text-violet-700 dark:bg-violet-950/50 dark:text-violet-300">
                {fact.source_media_type === "image" && <ImageIcon size={10} />}
                {fact.source_media_type === "pdf" && <FileText size={10} />}
                {fact.source_media_type === "video" && <Film size={10} />}
                {!["image", "pdf", "video"].includes(fact.source_media_type) && <FileText size={10} />}
                From {fact.source_media_type}
              </span>
            )}
            {fact.author_name && (
              <span className="text-xs text-muted-foreground">
                {fact.author_name}
              </span>
            )}
            {fact.message_ts && (
              <span className="text-xs text-muted-foreground">
                &middot; {formatTimestamp(fact.message_ts)}
              </span>
            )}
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
            {fact.action_tags.map((tag) => (
              <span
                key={tag}
                className="px-2 py-0.5 text-xs rounded-full bg-amber-100/60 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300"
              >
                {tag}
              </span>
            ))}
          </div>
          {fact.source_media_url && fact.source_media_type === "image" && (
            <img
              src={`${import.meta.env.VITE_API_URL || "http://localhost:8000"}/api/files/proxy?url=${encodeURIComponent(fact.source_media_url)}`}
              alt="Source media"
              className="w-20 h-20 rounded-lg border border-border object-cover"
              onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
            />
          )}
          {fact.source_message_id && (
            <div className="text-xs text-muted-foreground">
              <ExternalLink size={14} className="inline mr-1" />
              Source: {fact.source_message_id}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
