import type { WikiPageNode } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";

interface TopicCardProps {
  topic: WikiPageNode;
  onClick: () => void;
  lang?: string;
}

export function TopicCard({ topic, onClick, lang }: TopicCardProps) {
  return (
    <button
      onClick={onClick}
      className="text-left w-full rounded-xl border border-border bg-card p-4 hover:border-primary/40 hover:shadow-sm transition-all duration-150"
    >
      <div className="flex items-start justify-between gap-2 mb-1">
        <span className="text-xs text-muted-foreground/70 font-mono">{topic.section_number}</span>
        {topic.memory_count > 0 && (
          <span className="text-xs text-muted-foreground shrink-0">
            {wikiT(lang, "memoriesSuffix", { n: topic.memory_count })}
          </span>
        )}
      </div>
      <h3 className="text-sm font-semibold text-foreground leading-snug">{topic.title}</h3>
    </button>
  );
}
