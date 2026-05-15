/** Media gallery module — responsive grid of unpinned media items.
 *  Click an item to open it in a new tab (lightbox deferred). */
import { ImageIcon } from "lucide-react";
import type { ModuleProps } from "./ModuleRenderer";

interface GalleryItem {
  url?: string;
  alt?: string;
  caption?: string;
  kind?: string;
}

interface GalleryData {
  label?: string;
  items?: GalleryItem[];
}

export function MediaGalleryModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as GalleryData;
  const items = data.items ?? [];
  if (items.length === 0) return null;

  return (
    <section className="mt-8" id={`module-${module.anchor}`}>
      <h2 className="text-lg font-semibold text-foreground flex items-center gap-2 mb-3">
        <ImageIcon size={16} className="text-muted-foreground/70" />
        {data.label || "Gallery"}
        <span className="text-[11px] font-normal text-muted-foreground">
          ({items.length})
        </span>
      </h2>
      <div
        className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3"
        data-toc-skip
      >
        {items.map((item, idx) => {
          const url = (item.url || "").trim();
          if (!url) return null;
          return (
            <a
              key={`gallery-${idx}-${url}`}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="group block aspect-square overflow-hidden rounded-lg border border-border/60 bg-muted/30 hover:border-primary/40 transition-colors"
              title={item.caption || item.alt || ""}
            >
              <img
                src={url}
                alt={item.alt || ""}
                className="w-full h-full object-cover block group-hover:scale-105 transition-transform duration-200"
                loading="lazy"
              />
            </a>
          );
        })}
      </div>
    </section>
  );
}
