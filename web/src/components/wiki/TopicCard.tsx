import { ArrowRight, FileText } from "lucide-react";
import type { WikiPageNode } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";

interface TopicCardProps {
  topic: WikiPageNode;
  onClick: () => void;
  lang?: string;
}

export function TopicCard({ topic, onClick, lang }: TopicCardProps) {
  const summary = (topic.summary || "").trim();
  return (
    <button
      onClick={onClick}
      className="group text-left w-full rounded-xl border border-border bg-card p-4 hover:border-primary/40 hover:shadow-md hover:bg-card/80 transition-all duration-150 flex flex-col h-full"
    >
      {/* Top row — section number badge + memory count chip. Numbers
          read first (eye scans by digit), title second. */}
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="flex items-center gap-1.5">
          <span className="shrink-0 flex h-5 w-5 items-center justify-center rounded-md bg-muted text-muted-foreground/70 group-hover:bg-primary/10 group-hover:text-primary transition-colors">
            <FileText size={11} />
          </span>
          {topic.section_number && (
            <span className="text-[10.5px] text-muted-foreground/70 font-mono font-semibold tabular-nums">
              {topic.section_number}
            </span>
          )}
        </div>
        {topic.memory_count > 0 && (
          <span className="text-[11px] text-muted-foreground/80 shrink-0 tabular-nums">
            {wikiT(lang, "memoriesSuffix", { n: topic.memory_count })}
          </span>
        )}
      </div>
      {/* Title — primary affordance */}
      <h3 className="text-sm font-semibold text-foreground leading-snug group-hover:text-primary transition-colors line-clamp-2">
        {topic.title}
      </h3>
      {/* Summary — gives the user enough context to decide whether
          to click. Falls back gracefully when no summary is present
          (legacy pages, or topics whose serialiser hasn't populated
          it yet). */}
      {summary && (
        <p className="mt-2 text-[12px] text-muted-foreground leading-relaxed line-clamp-3 flex-1">
          {summary}
        </p>
      )}
      {/* Bottom row — Open hint anchored to bottom for visual rhythm */}
      <div className="mt-auto pt-3 flex items-center justify-end text-[11px] text-primary/60 group-hover:text-primary transition-colors">
        Open <ArrowRight size={11} className="ml-1" />
      </div>
    </button>
  );
}
