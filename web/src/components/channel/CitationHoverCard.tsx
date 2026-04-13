import {
  BookOpen,
  ExternalLink,
  Globe,
  Hash,
  Image as ImageIcon,
  MessageSquareText,
  Network,
  Paperclip,
  ScrollText,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { Source, SourceKind } from "@/types/askTypes";

interface CitationHoverCardProps {
  source: Source;
  /** Anchor rect from `getBoundingClientRect` of the triggering chip. */
  anchor: DOMRect;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
}

const KIND_ICONS: Record<SourceKind, LucideIcon> = {
  channel_message: Hash,
  media: ImageIcon,
  web_result: Globe,
  wiki_page: BookOpen,
  uploaded_file: Paperclip,
  qa_history: MessageSquareText,
  graph_relationship: Network,
  decision_record: ScrollText,
};

/**
 * Rich hover popover rendered in a portal-style fixed position. Shows
 * kind icon, title, metadata, excerpt (3-line clamp), and a permalink
 * when the source has an external URL.
 */
export function CitationHoverCard({
  source,
  anchor,
  onMouseEnter,
  onMouseLeave,
}: CitationHoverCardProps) {
  const Icon = KIND_ICONS[source.kind] ?? Hash;
  const hasExternal =
    typeof source.permalink === "string" &&
    /^https?:\/\//i.test(source.permalink);

  // Position below the chip; constrain left edge to viewport.
  // If the card would overflow the bottom of the viewport, flip above the anchor.
  const gap = 8;
  const width = 320;
  const viewportW = typeof window !== "undefined" ? window.innerWidth : 1024;
  const viewportH = typeof window !== "undefined" ? window.innerHeight : 768;
  const left = Math.max(8, Math.min(anchor.left, viewportW - width - 8));
  // Estimate card height for overflow check (real height unknown at render time).
  const estimatedCardHeight = 160;
  const top =
    anchor.bottom + estimatedCardHeight > viewportH
      ? anchor.top - estimatedCardHeight - gap
      : anchor.bottom + gap;

  return (
    <div
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      className={cn(
        "fixed z-50 rounded-md border border-border bg-popover",
        "text-popover-foreground shadow-md",
        "px-3 py-2.5 text-xs motion-safe:animate-scale-in origin-top-left",
      )}
      style={{ top, left, width, maxWidth: "calc(100vw - 16px)" }}
      role="tooltip"
    >
      <div className="flex items-center gap-2 mb-1">
        <Icon className="size-3 text-muted-foreground" strokeWidth={2} />
        <span className="font-medium text-foreground/90 truncate">
          {source.title}
        </span>
      </div>

      <Metadata source={source} />

      {source.excerpt && (
        <p className="mt-1.5 text-muted-foreground/80 leading-snug line-clamp-3">
          {source.excerpt}
        </p>
      )}

      {hasExternal && source.permalink && (
        <a
          href={source.permalink}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-2 inline-flex items-center gap-1 text-primary/80 hover:text-primary"
        >
          View original <ExternalLink className="size-2.5" />
        </a>
      )}
    </div>
  );
}

const UNAVAILABLE = new Set([
  "",
  "(unavailable)",
  "unavailable",
  "n/a",
  "na",
  "none",
  "null",
  "undefined",
]);

function disp(v: unknown): string | null {
  if (typeof v !== "string") return null;
  const t = v.trim();
  const lowered = t.toLowerCase();
  if (UNAVAILABLE.has(lowered)) return null;
  // Catch prefix-leaked variants like "# (unavailable)" / "@(unavailable)".
  const stripped = t.replace(/^[#@\s]+/, "").toLowerCase();
  if (UNAVAILABLE.has(stripped)) return null;
  return t;
}

function Metadata({ source }: { source: Source }) {
  const n = (source.native ?? {}) as Record<string, unknown>;
  const parts: string[] = [];
  switch (source.kind) {
    case "channel_message":
    case "media": {
      const rawAuthor = disp(n.author);
      // Hide platform-generated author ids (e.g. `xxx_user`).
      const author =
        rawAuthor && !/^[a-z0-9]{8,}_user$/i.test(rawAuthor) ? rawAuthor : null;
      const channel = disp(n.channel_name) ?? disp(n.channel_id);
      const ts = disp(n.timestamp);
      if (author) parts.push(`@${author.replace(/^@/, "")}`);
      if (channel) parts.push(`#${channel.replace(/^#/, "")}`);
      if (ts) parts.push(ts);
      break;
    }
    case "web_result": {
      const url = disp(n.url);
      if (url) {
        try {
          parts.push(new URL(url).host);
        } catch {
          parts.push(url);
        }
      }
      const published = disp(n.published_at);
      if (published) parts.push(published);
      break;
    }
    case "wiki_page": {
      const pt = disp(n.page_type);
      if (pt) parts.push(pt);
      break;
    }
    case "qa_history": {
      const t = disp(n.asked_at);
      if (t) parts.push(t);
      break;
    }
    case "uploaded_file": {
      const f = disp(n.filename);
      if (f) parts.push(f);
      break;
    }
    default:
      break;
  }
  if (parts.length === 0) return null;
  return (
    <div className="text-[10px] text-muted-foreground">
      {parts.join(" · ")}
    </div>
  );
}
