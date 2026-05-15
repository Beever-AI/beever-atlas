/** Video embed module — lazy iframe for YouTube/Vimeo, native
 *  ``<video>`` for direct file URLs. Multiple videos render as a
 *  vertical stack. Each iframe sets ``loading="lazy"`` so off-screen
 *  videos don't load on page mount. */
import { Video } from "lucide-react";
import type { ModuleProps } from "./ModuleRenderer";

interface VideoItem {
  url?: string;
  kind?: "youtube" | "vimeo" | "native" | string;
  title?: string;
}

interface VideoData {
  label?: string;
  items?: VideoItem[];
}

function youtubeEmbedUrl(url: string): string | null {
  // Match watch?v=ID, youtu.be/ID, or already-an-embed url.
  const watch = url.match(/[?&]v=([\w-]+)/);
  if (watch) return `https://www.youtube.com/embed/${watch[1]}`;
  const short = url.match(/youtu\.be\/([\w-]+)/);
  if (short) return `https://www.youtube.com/embed/${short[1]}`;
  if (url.includes("/embed/")) return url;
  return null;
}

function vimeoEmbedUrl(url: string): string | null {
  const m = url.match(/vimeo\.com\/(\d+)/);
  if (m) return `https://player.vimeo.com/video/${m[1]}`;
  return null;
}

export function VideoEmbedModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as VideoData;
  const items = data.items ?? [];
  if (items.length === 0) return null;

  return (
    <section className="mt-8" id={`module-${module.anchor}`}>
      <h2 className="text-lg font-semibold text-foreground flex items-center gap-2 mb-3">
        <Video size={16} className="text-muted-foreground/70" />
        {data.label || "Videos"}
        <span className="text-[11px] font-normal text-muted-foreground">
          ({items.length})
        </span>
      </h2>
      <div className="space-y-4" data-toc-skip>
        {items.map((item, idx) => {
          const url = (item.url || "").trim();
          if (!url) return null;
          const kind = item.kind ?? "native";
          let embedUrl: string | null = null;
          if (kind === "youtube") embedUrl = youtubeEmbedUrl(url);
          if (kind === "vimeo") embedUrl = vimeoEmbedUrl(url);
          return (
            <figure
              key={`video-${idx}-${url}`}
              className="rounded-lg overflow-hidden border border-border/60 bg-muted/20"
            >
              {kind === "native" ? (
                <video
                  src={url}
                  controls
                  className="w-full h-auto"
                  preload="metadata"
                  aria-label={item.title || "Video"}
                />
              ) : embedUrl ? (
                <div className="relative w-full" style={{ paddingBottom: "56.25%" }}>
                  <iframe
                    src={embedUrl}
                    title={item.title || "Embedded video"}
                    loading="lazy"
                    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                    allowFullScreen
                    className="absolute inset-0 w-full h-full border-0"
                  />
                </div>
              ) : (
                <a
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block p-4 text-sm text-primary hover:underline"
                >
                  {item.title || url}
                </a>
              )}
              {item.title && (
                <figcaption className="px-3 py-1.5 text-[11px] text-muted-foreground border-t border-border/40">
                  {item.title}
                </figcaption>
              )}
            </figure>
          );
        })}
      </div>
    </section>
  );
}
