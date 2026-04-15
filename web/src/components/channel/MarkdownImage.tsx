import { useState } from "react";
import { FileText, Image as ImageIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { isAuthGatedMediaUrl, mediaHostLabel, proxiedMediaUrl } from "@/lib/mediaUrl";

interface MarkdownImageProps {
  src?: string;
  alt?: string;
}

type MediaRole = "image" | "pdf" | "document";

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|svg|avif|heic|tiff?)(\?|#|$)/i;
const PDF_EXT = /\.pdf(\?|#|$)/i;
const DOC_EXT = /\.(docx?|xlsx?|pptx?|txt|csv|md|rtf|odt|ods|odp)(\?|#|$)/i;

function classifyUrl(url: string): MediaRole {
  // Path only — ignores query string by default, unless the query starts
  // with a filename (Slack `?filename=...` hint).
  let path = url;
  try {
    const u = new URL(url, "http://x");
    path = u.pathname + (u.search || "");
  } catch {
    /* fall through with raw url */
  }
  if (PDF_EXT.test(path)) return "pdf";
  if (IMAGE_EXT.test(path)) return "image";
  if (DOC_EXT.test(path)) return "document";
  return "image"; // Optimistic default — <img> + onError still handles misses.
}

/**
 * Renders markdown `![alt](url)` emitted by skills like media-gallery.
 * Dispatches by file type so PDFs get an inline preview, documents get a
 * file card, and images try `<img>` with a labeled-link fallback when the
 * browser refuses to load them.
 *
 * ReactMarkdown has no default `img` renderer — without this handler,
 * image nodes are silently dropped.
 */
export function MarkdownImage({ src, alt }: MarkdownImageProps) {
  const url = (src ?? "").trim();
  if (!url) return null;

  const role = classifyUrl(url);
  if (role === "pdf") return <PdfEmbed url={url} alt={alt} />;
  if (role === "document") return <DocumentCard url={url} alt={alt} />;
  return <ImageEmbed url={url} alt={alt} />;
}

// --- image path ------------------------------------------------------

function ImageEmbed({ url, alt }: { url: string; alt?: string }) {
  const initiallyAuthGated = isAuthGatedMediaUrl(url);
  const [errored, setErrored] = useState(initiallyAuthGated);

  if (errored) {
    const host = mediaHostLabel(url) ?? "image";
    return (
      <a
        href={url}
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
        <span className="truncate max-w-[20rem]">{alt || "Image"}</span>
        <span className="text-muted-foreground/70">· {host}</span>
      </a>
    );
  }

  const imgSrc = proxiedMediaUrl(url) ?? url;
  return (
    <span className="block my-3">
      <a href={url} target="_blank" rel="noopener noreferrer">
        <img
          src={imgSrc}
          alt={alt ?? ""}
          onError={() => setErrored(true)}
          className={cn(
            "max-h-80 rounded-md border border-border object-contain",
            "hover:ring-2 hover:ring-primary/40 transition-all",
          )}
        />
      </a>
      {alt ? (
        <span className="mt-1 block text-[11px] text-muted-foreground/70">
          {alt}
        </span>
      ) : null}
    </span>
  );
}

// --- pdf path --------------------------------------------------------

function PdfEmbed({ url, alt }: { url: string; alt?: string }) {
  const embedSrc = proxiedMediaUrl(url) ?? url;
  const host = mediaHostLabel(url) ?? "pdf";
  const label = alt || "PDF document";
  return (
    <span className="block my-3">
      <span className="block overflow-hidden rounded-md border border-border bg-card/40">
        <object
          data={embedSrc}
          type="application/pdf"
          className="block h-[420px] w-full"
          aria-label={label}
        >
          {/* Fallback when <object> can't render (e.g., Firefox with
              an auth error, or browsers without a PDF viewer). */}
          <span className="flex h-[120px] items-center justify-center px-4 text-xs text-muted-foreground">
            PDF preview unavailable — use the link below.
          </span>
        </object>
      </span>
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className={cn(
          "mt-2 inline-flex items-center gap-2 text-xs text-foreground/90",
          "hover:text-primary transition-colors",
        )}
      >
        <FileText className="size-3.5 text-muted-foreground" strokeWidth={2} />
        <span className="truncate max-w-[24rem] font-medium">{label}</span>
        <span className="text-muted-foreground/70">· {host}</span>
      </a>
    </span>
  );
}

// --- document path (docx, xlsx, pptx, ...) ---------------------------

function DocumentCard({ url, alt }: { url: string; alt?: string }) {
  const host = mediaHostLabel(url) ?? "file";
  const label = alt || "Document";
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className={cn(
        "my-2 inline-flex items-center gap-2 rounded-md border border-border",
        "bg-card/40 px-3 py-2 text-xs text-foreground/90",
        "hover:bg-muted/50 hover:border-primary/40 transition-colors",
      )}
      title={`${label} — click to open`}
    >
      <FileText className="size-3.5 text-muted-foreground" strokeWidth={2} />
      <span className="truncate max-w-[24rem]">{label}</span>
      <span className="text-muted-foreground/70">· {host}</span>
    </a>
  );
}
