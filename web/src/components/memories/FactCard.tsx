import { useState } from "react";
import { ChevronDown, ChevronRight, ExternalLink, ImageIcon, FileText, Film, ArrowRight } from "lucide-react";
import type { MemoryTier2 } from "@/lib/types";
import { MediaModal } from "@/components/graph/MediaModal";
import { buildLoaderUrl } from "@/lib/api";

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
  const [lightbox, setLightbox] = useState<{ url: string; name: string } | null>(null);

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
          <p className={`text-sm sm:text-[15px] leading-relaxed ${fact.superseded_by ? "text-muted-foreground line-through" : "text-foreground"}`}>
            {fact.memory_text}
          </p>
          {fact.superseded_by && (
            <p className="text-xs text-amber-600 dark:text-amber-400 mt-1 flex items-center gap-1">
              <ArrowRight size={10} />
              Superseded by a newer fact
            </p>
          )}
          {fact.thread_context_summary && (
            <p className="text-xs text-muted-foreground mt-1 italic">
              Thread: {fact.thread_context_summary}
            </p>
          )}
          <div className="flex items-center gap-1.5 mt-2 flex-wrap">
            {fact.fact_type && fact.fact_type !== "observation" && (
              <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-indigo-100 text-indigo-700 dark:bg-indigo-950/50 dark:text-indigo-300 capitalize">
                {fact.fact_type.replace("_", " ")}
              </span>
            )}
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
          {/* Media attachments (images, PDFs, videos) */}
          {(fact.source_media_urls?.length > 0 || fact.source_media_url) && (
            <div className="flex flex-wrap gap-2">
              {(fact.source_media_urls?.length > 0 ? fact.source_media_urls : [fact.source_media_url].filter(Boolean)).map((url, i) => {
                const proxyUrl = buildLoaderUrl(`/api/files/proxy?url=${encodeURIComponent(url)}`);
                const isImage = fact.source_media_type === "image" || url.match(/\.(png|jpg|jpeg|gif|webp)(\?|$)/i);
                if (isImage) {
                  return (
                    <img
                      key={url}
                      src={proxyUrl}
                      alt="Source media"
                      className="w-20 h-20 rounded-lg border border-border object-cover cursor-pointer hover:ring-2 hover:ring-primary/50 transition-all"
                      onClick={() => setLightbox({ url, name: "Image" })}
                      onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                    />
                  );
                }
                const isPdf = fact.source_media_type === "pdf" || url.match(/\.pdf(\?|$)/i);
                return (
                  <a
                    key={url}
                    href={proxyUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg border border-border bg-muted/50 hover:bg-muted text-foreground transition-colors"
                  >
                    {isPdf ? <FileText size={12} /> : <Film size={12} />}
                    {isPdf ? "View PDF" : "View file"}{fact.source_media_urls?.length > 1 ? ` (${i + 1})` : ""}
                  </a>
                );
              })}
            </div>
          )}
          {/* Shared links/URLs */}
          {fact.source_link_urls?.length > 0 && (
            <div className="space-y-1">
              {fact.source_link_urls.map((url, i) => (
                <a
                  key={url}
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-1.5 text-xs text-primary hover:underline"
                >
                  <ExternalLink size={12} />
                  {fact.source_link_titles?.[i] || url}
                </a>
              ))}
            </div>
          )}
          {fact.source_message_id && (
            <div className="text-xs text-muted-foreground">
              <ExternalLink size={14} className="inline mr-1" />
              Source: {fact.source_message_id}
            </div>
          )}
        </div>
      )}
      {lightbox && (
        <MediaModal
          name={lightbox.name}
          url={lightbox.url}
          mediaType="image"
          onClose={() => setLightbox(null)}
        />
      )}
    </div>
  );
}
