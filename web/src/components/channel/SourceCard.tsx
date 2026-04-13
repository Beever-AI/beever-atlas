import {
  BookOpen,
  ExternalLink,
  FileText,
  Film,
  Globe,
  Hash,
  Image as ImageIcon,
  MessageSquareText,
  Music,
  Network,
  Paperclip,
  ScrollText,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";
import type {
  MediaAttachment,
  Source,
  SourceKind,
} from "@/types/askTypes";
import { DerivedFrom } from "./DerivedFrom";
import { isAuthGatedMediaUrl } from "@/lib/mediaUrl";

interface SourceCardProps {
  source: Source;
  /** Index in the footer list — used to build the scroll-anchor id. */
  index: number;
  /** Markers that reference this source. Count used for the "Cited Nx" badge. */
  refCount: number;
  messageId: string;
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

const MEDIA_KIND_ICONS: Record<MediaAttachment["kind"], LucideIcon> = {
  image: ImageIcon,
  pdf: FileText,
  document: FileText,
  link_preview: Globe,
  video: Film,
  audio: Music,
};

/**
 * One card per unique source in the Sources footer. Kind dispatches the
 * icon and metadata layout; attachments render a compact preview strip.
 *
 * The card's `id` and `data-citation-id` attributes are the scroll
 * targets for `chat:citation-jump` events fired by inline `[N]` chips.
 */
export function SourceCard({ source, index, refCount, messageId }: SourceCardProps) {
  const Icon = KIND_ICONS[source.kind] ?? Hash;
  const hasExternalPermalink =
    typeof source.permalink === "string" &&
    /^https?:\/\//i.test(source.permalink);

  return (
    <div
      id={`citation-${messageId}-${index}`}
      data-citation-id={`${messageId}-${index}`}
      className="flex gap-3 rounded-md border border-border/60 bg-card/40 px-3 py-2.5 text-xs transition-all"
    >
      <span className="mt-0.5 font-mono text-muted-foreground/70 w-6 shrink-0">
        [{index}]
      </span>

      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Icon className="size-3 text-muted-foreground" strokeWidth={2} />
          <SourceMetadata source={source} />
          {refCount > 1 && (
            <span
              className={cn(
                "ml-auto inline-flex items-center rounded-full",
                "bg-muted/70 px-1.5 py-0.5 text-[10px] font-medium",
                "text-muted-foreground/80",
              )}
              title={`Cited ${refCount} times in this answer`}
            >
              Cited {refCount}×
            </span>
          )}
        </div>

        {source.excerpt && (
          <p className="text-muted-foreground/80 mt-1 leading-relaxed line-clamp-2">
            {source.excerpt}
          </p>
        )}

        {source.attachments.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {source.attachments.slice(0, 3).map((a, i) => (
              <AttachmentChip key={`${a.url}-${i}`} attachment={a} />
            ))}
            {source.attachments.length > 3 && (
              <span className="text-[10px] text-muted-foreground/60">
                +{source.attachments.length - 3} more
              </span>
            )}
          </div>
        )}

        {hasExternalPermalink && source.permalink && (
          <a
            href={source.permalink}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-2 inline-flex items-center gap-1 text-primary/80 hover:text-primary"
          >
            View original <ExternalLink className="size-2.5" />
          </a>
        )}

        {source.kind === "qa_history" && (
          <DerivedFrom
            priorCitations={
              (source.native as Record<string, unknown>)
                ?.prior_citations as Parameters<typeof DerivedFrom>[0]["priorCitations"] ?? []
            }
          />
        )}
      </div>
    </div>
  );
}

/** Raw-string values we treat as "no usable value" for display fields. */
const UNAVAILABLE_VALUES = new Set([
  "",
  "(unavailable)",
  "unavailable",
  "n/a",
  "na",
  "none",
  "null",
  "undefined",
]);

/** Matches platform-generated author ids that leaked into `author_name`
 * (e.g. `edZtxpxrofbao_user`, `u123456789_user`). We hide these rather
 * than rendering them as a user-facing display name. */
const AUTO_AUTHOR_ID_RE = /^[a-z0-9]{8,}_user$/i;

/** Trim + filter sentinel values; return null when the string is not useful.
 *
 * Also catches prefix-leaked variants like `"# (unavailable)"` that arise
 * when a component concats the prefix before filtering.
 */
function displayValue(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  const lowered = trimmed.toLowerCase();
  if (UNAVAILABLE_VALUES.has(lowered)) return null;
  // Strip a leading #/@/whitespace before re-checking, so values like
  // `"# (unavailable)"` or `"@(unavailable)"` also get filtered.
  const stripped = trimmed.replace(/^[#@\s]+/, "").toLowerCase();
  if (UNAVAILABLE_VALUES.has(stripped)) return null;
  return trimmed;
}

/** Stricter variant for author fields: also rejects auto-generated ids. */
function displayAuthor(raw: unknown): string | null {
  const value = displayValue(raw);
  if (!value) return null;
  if (AUTO_AUTHOR_ID_RE.test(value)) return null;
  return value;
}

function SourceMetadata({ source }: { source: Source }) {
  const n = (source.native ?? {}) as Record<string, unknown>;
  switch (source.kind) {
    case "channel_message": {
      const author = displayAuthor(n.author);
      const channel = displayValue(n.channel_name) ?? displayValue(n.channel_id);
      const ts = displayValue(n.timestamp);
      return (
        <>
          {author && <span className="text-foreground/90 font-medium">{author}</span>}
          {channel && (
            <span className="inline-flex items-center gap-0.5 text-muted-foreground">
              <Hash className="size-2.5" />
              {channel.replace(/^#/, "")}
            </span>
          )}
          {ts && <span className="text-muted-foreground/70">{ts}</span>}
        </>
      );
    }
    case "web_result": {
      const url = displayValue(n.url);
      let host = "";
      if (url) {
        try {
          host = new URL(url).host;
        } catch {
          host = "";
        }
      }
      const published = displayValue(n.published_at);
      return (
        <>
          <span className="text-foreground/90 font-medium">{source.title}</span>
          {host && <span className="text-muted-foreground">{host}</span>}
          {published && (
            <span className="text-muted-foreground/70">{published}</span>
          )}
        </>
      );
    }
    case "wiki_page": {
      const pageType = displayValue(n.page_type);
      return (
        <>
          <span className="text-foreground/90 font-medium">{source.title}</span>
          {pageType && (
            <span className="text-muted-foreground">{pageType}</span>
          )}
        </>
      );
    }
    case "uploaded_file": {
      const filename = displayValue(n.filename);
      return (
        <span className="text-foreground/90 font-medium truncate">
          {filename ?? source.title}
        </span>
      );
    }
    case "media": {
      const channel = displayValue(n.channel_name) ?? displayValue(n.channel_id);
      const ts = displayValue(n.timestamp);
      return (
        <>
          <span className="text-foreground/90 font-medium">{source.title}</span>
          {channel && (
            <span className="inline-flex items-center gap-0.5 text-muted-foreground">
              <Hash className="size-2.5" />
              {channel.replace(/^#/, "")}
            </span>
          )}
          {ts && <span className="text-muted-foreground/70">{ts}</span>}
        </>
      );
    }
    case "qa_history": {
      const askedAt = displayValue(n.asked_at);
      return (
        <>
          <span className="text-foreground/90 font-medium truncate">
            {source.title}
          </span>
          {askedAt && (
            <span className="text-muted-foreground/70">{askedAt}</span>
          )}
        </>
      );
    }
    case "graph_relationship":
    case "decision_record":
    default: {
      return (
        <span className="text-foreground/90 font-medium truncate">
          {source.title}
        </span>
      );
    }
  }
}

function AttachmentChip({ attachment }: { attachment: MediaAttachment }) {
  const Icon = MEDIA_KIND_ICONS[attachment.kind] ?? Paperclip;
  if (attachment.kind === "image") {
    return <ImageChip attachment={attachment} />;
  }
  return (
    <a
      href={attachment.url}
      target="_blank"
      rel="noopener noreferrer"
      className={cn(
        "inline-flex items-center gap-1 rounded border border-border",
        "bg-muted/40 px-1.5 py-0.5 text-[10px] text-muted-foreground",
        "hover:bg-muted/60 hover:border-primary/40 transition-colors",
        "max-w-[16rem] truncate",
      )}
      title={attachment.title ?? attachment.filename ?? attachment.url}
    >
      <Icon className="size-2.5" strokeWidth={2} />
      <span className="truncate">
        {attachment.filename ?? attachment.title ?? attachment.kind}
      </span>
    </a>
  );
}

/** Image preview that degrades to an icon-only link for auth-gated hosts
 * (Slack files, Discord CDN, etc.) or when the `<img>` fails to load. */
function ImageChip({ attachment }: { attachment: MediaAttachment }) {
  const initiallyBlocked = isAuthGatedMediaUrl(
    attachment.thumbnail_url ?? attachment.url,
  );
  const [errored, setErrored] = useState(initiallyBlocked);

  if (errored) {
    return (
      <a
        href={attachment.url}
        target="_blank"
        rel="noopener noreferrer"
        className={cn(
          "inline-flex h-10 w-10 items-center justify-center rounded border border-border",
          "bg-muted/40 text-muted-foreground hover:border-primary/40 transition-colors",
        )}
        title={attachment.alt_text ?? attachment.title ?? "image (opens in new tab)"}
      >
        <ImageIcon className="size-4" strokeWidth={2} />
      </a>
    );
  }

  return (
    <a
      href={attachment.url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-block h-10 w-10 overflow-hidden rounded border border-border hover:border-primary/40 transition-colors"
      title={attachment.alt_text ?? attachment.title ?? "image"}
    >
      <img
        src={attachment.thumbnail_url ?? attachment.url}
        alt={attachment.alt_text ?? "attachment"}
        onError={() => setErrored(true)}
        className="h-full w-full object-cover"
      />
    </a>
  );
}

