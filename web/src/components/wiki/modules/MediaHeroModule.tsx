/** Media hero module — single centerpiece image or video, full-width
 *  at the top of the body. Used for topics where one media item is
 *  the obvious primary visual (e.g., a key dashboard, a meeting
 *  recording's screenshot). */
import type { ModuleProps } from "./ModuleRenderer";

interface HeroData {
  label?: string;
  url?: string;
  alt?: string;
  caption?: string;
  source_author?: string;
  source_date?: string;
  kind?: string;
}

export function MediaHeroModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as HeroData;
  const url = (data.url || "").trim();
  if (!url) return null;

  const isVideo = data.kind === "video" || /\.(mp4|webm)$/i.test(url);
  const attribution = [data.source_author, data.source_date]
    .filter(Boolean)
    .join(" · ");

  return (
    <section className="mt-8" id={`module-${module.anchor}`} data-toc-skip>
      <figure className="rounded-xl overflow-hidden border border-border bg-muted/30">
        {isVideo ? (
          <video
            src={url}
            controls
            className="w-full h-auto"
            preload="metadata"
            aria-label={data.alt || "Hero video"}
          />
        ) : (
          <img
            src={url}
            alt={data.alt || ""}
            className="w-full h-auto block"
            loading="lazy"
          />
        )}
        {(data.caption || attribution) && (
          <figcaption className="px-4 py-2 text-xs text-muted-foreground border-t border-border/60">
            {data.caption && <span className="block">{data.caption}</span>}
            {attribution && (
              <span className="block text-muted-foreground/70 mt-0.5">
                {attribution}
              </span>
            )}
          </figcaption>
        )}
      </figure>
    </section>
  );
}
