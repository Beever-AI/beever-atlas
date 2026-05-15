/** Tool card grid — replaces a flat bullet list with compact cards.
 *  Each card has a kind-themed icon (heuristic from tool name) plus
 *  a one-line "used for" description. The icon recognises common
 *  developer tools so the eye scans by glyph; falls back to a
 *  generic Wrench glyph for unknown names. */
import {
  Database,
  GitBranch,
  Globe,
  MessageCircle,
  Settings,
  Wrench,
  type LucideIcon,
} from "lucide-react";

export interface ToolEntry {
  name: string;
  /** One-line "used for" — extracted from the LLM bullet body. */
  description: string;
}

const _ICON_HINTS: Array<[RegExp, LucideIcon]> = [
  [/git\b|github|gitlab|bitbucket/i, GitBranch],
  [/mattermost|slack|discord|teams|telegram/i, MessageCircle],
  [/mongo|postgres|mysql|redis|sqlite|neo4j|nebula|weaviate|sql/i, Database],
  [/fastapi|django|flask|express|nestjs|next\.js|vite|webpack|node\.?js|python/i, Settings],
  [/aws|gcp|azure|cloud|vercel|netlify|render/i, Globe],
];

function iconFor(name: string): LucideIcon {
  for (const [re, icon] of _ICON_HINTS) {
    if (re.test(name)) return icon;
  }
  return Wrench;
}

interface ToolCardProps {
  entry: ToolEntry;
}

export function ToolCard({ entry }: ToolCardProps) {
  const Icon = iconFor(entry.name);
  return (
    <div className="rounded-xl border border-border bg-card p-3 hover:border-primary/30 transition-colors flex gap-3">
      <span
        className="shrink-0 flex h-8 w-8 items-center justify-center rounded-md bg-muted text-muted-foreground/80"
        aria-hidden="true"
      >
        <Icon size={14} />
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-semibold text-foreground line-clamp-1">
          {entry.name}
        </div>
        {entry.description && (
          <p className="text-[12px] text-muted-foreground leading-relaxed line-clamp-2 mt-0.5">
            {entry.description}
          </p>
        )}
      </div>
    </div>
  );
}

interface ToolGridProps {
  tools: ToolEntry[];
  title?: string;
}

export function ToolGrid({ tools, title = "Tools & Resources" }: ToolGridProps) {
  if (tools.length === 0) return null;
  return (
    <div className="mt-8" data-toc-skip>
      <h2 className="text-lg font-semibold text-foreground mb-3 flex items-center gap-2">
        {title}
        <span className="text-[11px] font-normal text-muted-foreground">
          ({tools.length})
        </span>
      </h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {tools.map((t, i) => (
          <ToolCard key={`tool-${i}-${t.name}`} entry={t} />
        ))}
      </div>
    </div>
  );
}
