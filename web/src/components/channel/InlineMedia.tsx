import { useState } from "react";
import { FileText, Film, Globe, Image as ImageIcon, Music } from "lucide-react";
import { cn } from "@/lib/utils";
import type { MediaAttachment, Source } from "@/types/askTypes";
import { isAuthGatedMediaUrl, mediaHostLabel, proxiedMediaUrl } from "@/lib/mediaUrl";
import { looksUnavailable } from "@/lib/citations";

function safeLabel(raw: unknown, fallback: string): string {
  if (typeof raw !== "string" || !raw.trim() || looksUnavailable(raw)) return fallback;
  return raw.trim();
}

interface InlineMediaProps {
  attachment: MediaAttachment;
  source: Source;
  n: number;
  messageId: string;
}

/**
 * Render a media attachment inline at the position of a `[N]` marker
 * when `refs[N].inline === true`. Clicking routes to the source
 * permalink (if external) or the attachment URL.
 */
export function InlineMedia({ attachment, source, n, messageId }: InlineMediaProps) {
  const jumpToFooter = (e: React.MouseEvent) => {
    // Only jump to footer when the user clicks on the caption or the
    // background — the image/link card itself opens its target.
    if ((e.target as HTMLElement).dataset?.role === "caption") {
      e.preventDefault();
      window.dispatchEvent(
        new CustomEvent("chat:citation-jump", { detail: { index: n, messageId } }),
      );
    }
  };

  const openHref = attachment.url;

  if (attachment.kind === "image") {
    return (
      <InlineImage
        attachment={attachment}
        source={source}
        n={n}
        openHref={openHref}
        onJumpToFooter={jumpToFooter}
      />
    );
  }

  if (attachment.kind === "pdf" || attachment.kind === "document") {
    return (
      <a
        href={openHref}
        target="_blank"
        rel="noopener noreferrer"
        className={cn(
          "my-2 inline-flex items-center gap-2 rounded-md border border-border",
          "bg-card/40 px-3 py-2 text-xs text-foreground/90",
          "hover:bg-muted/50 hover:border-primary/40 transition-colors",
        )}
        title={attachment.filename ?? source.title}
      >
        <FileText className="size-3.5 text-muted-foreground" strokeWidth={2} />
        <span className="truncate max-w-[16rem]">
          {safeLabel(
            attachment.filename ?? attachment.title ?? source.title,
            "Document",
          )}
        </span>
        <span className="text-muted-foreground/70">[{n}]</span>
      </a>
    );
  }

  if (attachment.kind === "link_preview") {
    let host = "";
    try {
      host = new URL(attachment.url).host;
    } catch {
      host = attachment.url;
    }
    return (
      <a
        href={openHref}
        target="_blank"
        rel="noopener noreferrer"
        className={cn(
          "my-2 inline-flex items-center gap-2 rounded-md border border-border",
          "bg-card/40 px-3 py-2 text-xs text-foreground/90",
          "hover:bg-muted/50 hover:border-primary/40 transition-colors",
        )}
      >
        <Globe className="size-3.5 text-muted-foreground" strokeWidth={2} />
        <span className="truncate max-w-[18rem]">
          {safeLabel(attachment.title, host)}
        </span>
        <span className="text-muted-foreground/70">[{n}]</span>
      </a>
    );
  }

  // video / audio: render a link card, not an inline player.
  const Icon = attachment.kind === "audio" ? Music : Film;
  return (
    <a
      href={openHref}
      target="_blank"
      rel="noopener noreferrer"
      className={cn(
        "my-2 inline-flex items-center gap-2 rounded-md border border-border",
        "bg-card/40 px-3 py-2 text-xs text-foreground/90",
        "hover:bg-muted/50 hover:border-primary/40 transition-colors",
      )}
    >
      <Icon className="size-3.5 text-muted-foreground" strokeWidth={2} />
      <span className="truncate max-w-[16rem]">
        {safeLabel(attachment.title ?? source.title, attachment.kind)}
      </span>
      <span className="text-muted-foreground/70">[{n}]</span>
    </a>
  );
}

// ----- Inline image with fallback when the URL is auth-gated or errors -----

interface InlineImageProps {
  attachment: MediaAttachment;
  source: Source;
  n: number;
  openHref: string;
  onJumpToFooter: (e: React.MouseEvent) => void;
}

function InlineImage({
  attachment,
  source,
  n,
  openHref,
  onJumpToFooter,
}: InlineImageProps) {
  // Start in "broken" mode for known auth-gated hosts so the browser
  // doesn't even try to fetch them and show a broken-image icon.
  const initiallyAuthGated = isAuthGatedMediaUrl(attachment.url);
  const [errored, setErrored] = useState(initiallyAuthGated);

  if (errored) {
    const host = mediaHostLabel(attachment.url) ?? "image";
    return (
      <a
        href={openHref}
        target="_blank"
        rel="noopener noreferrer"
        className={cn(
          "my-2 inline-flex items-center gap-2 rounded-md border border-border",
          "bg-card/40 px-3 py-2 text-xs text-foreground/90",
          "hover:bg-muted/50 hover:border-primary/40 transition-colors",
        )}
        title={
          initiallyAuthGated
            ? `${host} requires sign-in to view — click to open`
            : "Image failed to load — click to open"
        }
      >
        <ImageIcon className="size-3.5 text-muted-foreground" strokeWidth={2} />
        <span className="truncate max-w-[16rem]">
          {safeLabel(
            attachment.title ?? attachment.alt_text ?? source.title,
            "Image",
          )}
        </span>
        <span className="text-muted-foreground/70">· {host}</span>
        <span className="text-muted-foreground/70">[{n}]</span>
      </a>
    );
  }

  const imgSrc = proxiedMediaUrl(attachment.url) ?? attachment.url;
  return (
    <span className="block my-3" onClick={onJumpToFooter}>
      <a href={openHref} target="_blank" rel="noopener noreferrer">
        <img
          src={imgSrc}
          alt={attachment.alt_text ?? source.title ?? "Source image"}
          onError={() => setErrored(true)}
          className={cn(
            "max-h-80 rounded-md border border-border object-contain",
            "hover:ring-2 hover:ring-primary/40 transition-all",
          )}
        />
      </a>
      <span
        data-role="caption"
        className="mt-1 block text-[11px] text-muted-foreground/70 cursor-pointer hover:text-foreground"
      >
        [{n}] {safeLabel(source.title, "Image")}
      </span>
    </span>
  );
}
