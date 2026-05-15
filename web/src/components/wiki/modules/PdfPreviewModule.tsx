/** PDF preview module — document attachments rendered with thumbnail
 *  (when available) + Open button. Each card opens the PDF in a new
 *  tab via the file proxy. */
import { FileText, ExternalLink } from "lucide-react";
import type { ModuleProps } from "./ModuleRenderer";

interface PdfItem {
  url?: string;
  title?: string;
  thumbnail_url?: string;
}

interface PdfData {
  label?: string;
  items?: PdfItem[];
}

export function PdfPreviewModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as PdfData;
  const items = data.items ?? [];
  if (items.length === 0) return null;

  return (
    <section className="mt-8" id={`module-${module.anchor}`}>
      <h2 className="text-lg font-semibold text-foreground flex items-center gap-2 mb-3">
        <FileText size={16} className="text-muted-foreground/70" />
        {data.label || "Documents"}
        <span className="text-[11px] font-normal text-muted-foreground">
          ({items.length})
        </span>
      </h2>
      <div
        className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3"
        data-toc-skip
      >
        {items.map((item, idx) => {
          const url = (item.url || "").trim();
          if (!url) return null;
          const filename = item.title || decodeURIComponent(url.split("/").pop() || "Document");
          return (
            <a
              key={`pdf-${idx}-${url}`}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="group flex flex-col rounded-lg border border-border/60 bg-card overflow-hidden hover:border-primary/40 hover:shadow-sm transition-all"
            >
              <div className="aspect-[4/3] bg-muted/40 flex items-center justify-center border-b border-border/40">
                {item.thumbnail_url ? (
                  <img
                    src={item.thumbnail_url}
                    alt=""
                    className="w-full h-full object-cover"
                    loading="lazy"
                  />
                ) : (
                  <FileText size={36} className="text-muted-foreground/40" />
                )}
              </div>
              <div className="p-3 flex items-center justify-between gap-2">
                <span className="text-xs font-medium text-foreground line-clamp-2 flex-1 group-hover:text-primary transition-colors">
                  {filename}
                </span>
                <ExternalLink
                  size={12}
                  className="shrink-0 text-muted-foreground/60 group-hover:text-primary transition-colors"
                />
              </div>
            </a>
          );
        })}
      </div>
    </section>
  );
}
