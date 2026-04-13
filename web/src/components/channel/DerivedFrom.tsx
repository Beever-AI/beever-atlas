import { useState } from "react";
import { ChevronDown, Hash } from "lucide-react";
import { cn } from "@/lib/utils";

interface PriorCitation {
  id?: string | null;
  kind?: string;
  title?: string;
  author?: string | null;
  channel?: string | null;
  timestamp?: string | null;
}

interface DerivedFromProps {
  priorCitations: PriorCitation[];
}

/**
 * A sub-row rendered inside a `qa_history` SourceCard showing the
 * underlying citations the original answer referenced. Collapsed by
 * default — one level deep only (no recursion).
 */
export function DerivedFrom({ priorCitations }: DerivedFromProps) {
  const [expanded, setExpanded] = useState(false);
  if (!priorCitations || priorCitations.length === 0) return null;

  return (
    <div className="mt-2">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
        aria-expanded={expanded}
      >
        <ChevronDown
          className={cn(
            "size-3 transition-transform",
            expanded ? "rotate-0" : "-rotate-90",
          )}
        />
        <span>Derived from ({priorCitations.length})</span>
      </button>
      {expanded && (
        <div className="mt-1.5 flex flex-col gap-1 border-l border-border/60 pl-2 ml-1">
          {priorCitations.map((pc, i) => (
            <PriorRow key={`${pc.id ?? "legacy"}-${i}`} pc={pc} />
          ))}
        </div>
      )}
    </div>
  );
}

function PriorRow({ pc }: { pc: PriorCitation }) {
  const author = (pc.author ?? "").trim();
  const channel = (pc.channel ?? "").trim();
  const ts = (pc.timestamp ?? "").trim();
  return (
    <div className="flex flex-wrap items-center gap-x-2 text-[11px]">
      {author && (
        <span className="text-foreground/80">
          @{author.replace(/^@/, "")}
        </span>
      )}
      {channel && (
        <span className="inline-flex items-center gap-0.5 text-muted-foreground/80">
          <Hash className="size-2.5" />
          {channel.replace(/^#/, "")}
        </span>
      )}
      {ts && <span className="text-muted-foreground/60">{ts}</span>}
      {!author && !channel && (
        <span className="text-muted-foreground/70 truncate max-w-[20rem]">
          {pc.title ?? "source"}
        </span>
      )}
    </div>
  );
}
