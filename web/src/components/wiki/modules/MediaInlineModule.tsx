/** Media inline module — image(s) or video(s) pinned to specific
 *  facts. Each item renders as a sized embed with caption. The
 *  planner placed the marker adjacent to the prose discussing the
 *  source fact, so visual placement carries semantic weight. */
import type { ModuleProps } from "./ModuleRenderer";

interface InlineItem {
  media_id?: string;
  url?: string;
  alt?: string;
  caption?: string;
  fact_id?: string;
  kind?: string;
}

interface InlineData {
  label?: string;
  items?: InlineItem[];
}

export function MediaInlineModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as InlineData;
  const items = data.items ?? [];
  if (items.length === 0) return null;

  return (
    <section className="mt-6 space-y-4" id={`module-${module.anchor}`} data-toc-skip>
      {items.map((item, idx) => {
        const url = (item.url || "").trim();
        if (!url) return null;
        const isVideo = item.kind === "video" || /\.(mp4|webm)$/i.test(url);
        return (
          <figure
            key={item.media_id || `inline-${idx}`}
            className="max-w-2xl rounded-lg overflow-hidden border border-border/60 bg-muted/20"
          >
            {isVideo ? (
              <video
                src={url}
                controls
                className="w-full h-auto"
                preload="metadata"
                aria-label={item.alt || "Inline video"}
              />
            ) : (
              <img
                src={url}
                alt={item.alt || ""}
                className="w-full h-auto block"
                loading="lazy"
              />
            )}
            {item.caption && (
              <figcaption className="px-3 py-1.5 text-[11px] text-muted-foreground border-t border-border/40">
                {item.caption}
              </figcaption>
            )}
          </figure>
        );
      })}
    </section>
  );
}
