/** Link card module — external URLs (articles, repos, docs) rendered
 *  as compact cards with title + description. Favicon decoration when
 *  the data carries one; otherwise a generic Link icon. */
import { Link2, ExternalLink } from "lucide-react";
import type { ModuleProps } from "./ModuleRenderer";

interface LinkItem {
  url?: string;
  title?: string;
  description?: string;
  favicon?: string;
}

interface LinkData {
  label?: string;
  items?: LinkItem[];
}

export function LinkCardModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as LinkData;
  const items = data.items ?? [];
  if (items.length === 0) return null;

  return (
    <section className="mt-8" id={`module-${module.anchor}`}>
      <h2 className="text-lg font-semibold text-foreground flex items-center gap-2 mb-3">
        <Link2 size={16} className="text-muted-foreground/70" />
        {data.label || "Linked resources"}
        <span className="text-[11px] font-normal text-muted-foreground">
          ({items.length})
        </span>
      </h2>
      <div className="space-y-2" data-toc-skip>
        {items.map((item, idx) => {
          const url = (item.url || "").trim();
          if (!url) return null;
          return (
            <a
              key={`link-${idx}-${url}`}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="group block rounded-lg border border-border/60 bg-card p-3 hover:border-primary/40 hover:bg-muted/40 transition-colors"
            >
              <div className="flex items-start gap-3">
                {item.favicon ? (
                  <img
                    src={item.favicon}
                    alt=""
                    className="w-5 h-5 rounded shrink-0 mt-0.5"
                    loading="lazy"
                  />
                ) : (
                  <span className="shrink-0 mt-0.5 flex h-5 w-5 items-center justify-center rounded bg-muted text-muted-foreground/70">
                    <Link2 size={11} />
                  </span>
                )}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5 text-sm font-medium text-foreground group-hover:text-primary transition-colors">
                    <span className="line-clamp-1">{item.title || url}</span>
                    <ExternalLink size={11} className="shrink-0 text-muted-foreground/60" />
                  </div>
                  {item.description && (
                    <p className="text-[12px] text-muted-foreground mt-0.5 line-clamp-2">
                      {item.description}
                    </p>
                  )}
                </div>
              </div>
            </a>
          );
        })}
      </div>
    </section>
  );
}
