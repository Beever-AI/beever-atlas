import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "lucide-react";

interface PaginationProps {
  page: number;
  pages: number;
  total: number;
  pageSize: number;
  onPage: (page: number) => void;
  disabled?: boolean;
}

function pageWindow(page: number, pages: number): (number | "…")[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
  const out: (number | "…")[] = [1];
  const left = Math.max(2, page - 1);
  const right = Math.min(pages - 1, page + 1);
  if (left > 2) out.push("…");
  for (let i = left; i <= right; i++) out.push(i);
  if (right < pages - 1) out.push("…");
  out.push(pages);
  return out;
}

/**
 * Page-based pagination control for atomic-facts list. Renders first/prev/
 * window/next/last with ellipsis when total page count exceeds 7. Compact
 * enough to fit at both top and bottom of a long list.
 */
export function Pagination({
  page,
  pages,
  total,
  pageSize,
  onPage,
  disabled,
}: PaginationProps) {
  if (pages <= 1) return null;
  const win = pageWindow(page, pages);
  const start = (page - 1) * pageSize + 1;
  const end = Math.min(page * pageSize, total);

  const btnBase =
    "inline-flex h-8 min-w-[2rem] items-center justify-center rounded-md border border-border bg-background px-2 text-sm font-medium transition disabled:opacity-40";
  const btnActive = "border-foreground bg-foreground text-background hover:bg-foreground/90";
  const btnInactive = "hover:bg-muted text-foreground";

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 py-1">
      <span className="text-xs text-muted-foreground">
        Showing {start.toLocaleString()}–{end.toLocaleString()} of {total.toLocaleString()}
      </span>
      <div className="flex items-center gap-1">
        <button
          type="button"
          aria-label="First page"
          onClick={() => onPage(1)}
          disabled={disabled || page === 1}
          className={`${btnBase} ${btnInactive}`}
        >
          <ChevronsLeft className="h-4 w-4" aria-hidden />
        </button>
        <button
          type="button"
          aria-label="Previous page"
          onClick={() => onPage(page - 1)}
          disabled={disabled || page === 1}
          className={`${btnBase} ${btnInactive}`}
        >
          <ChevronLeft className="h-4 w-4" aria-hidden />
        </button>
        {win.map((p, idx) =>
          p === "…" ? (
            <span
              key={`gap-${idx}`}
              className="px-1 text-sm text-muted-foreground"
              aria-hidden
            >
              …
            </span>
          ) : (
            <button
              key={p}
              type="button"
              onClick={() => onPage(p)}
              disabled={disabled}
              aria-current={p === page ? "page" : undefined}
              className={`${btnBase} ${p === page ? btnActive : btnInactive}`}
            >
              {p}
            </button>
          ),
        )}
        <button
          type="button"
          aria-label="Next page"
          onClick={() => onPage(page + 1)}
          disabled={disabled || page === pages}
          className={`${btnBase} ${btnInactive}`}
        >
          <ChevronRight className="h-4 w-4" aria-hidden />
        </button>
        <button
          type="button"
          aria-label="Last page"
          onClick={() => onPage(pages)}
          disabled={disabled || page === pages}
          className={`${btnBase} ${btnInactive}`}
        >
          <ChevronsRight className="h-4 w-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}
