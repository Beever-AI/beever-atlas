import { useEffect, useState, useRef } from "react";

interface TocItem {
  id: string;
  text: string;
  level: number; // 2 or 3
}

interface WikiTableOfContentsProps {
  contentRef: React.RefObject<HTMLDivElement | null>;
}

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, "")
    .replace(/\s+/g, "-")
    .slice(0, 80);
}

export function extractHeadings(container: HTMLElement): TocItem[] {
  const headings = container.querySelectorAll("h2, h3");
  const items: TocItem[] = [];

  headings.forEach((el) => {
    // Skip headings inside any ancestor marked ``data-toc-skip``. Used
    // by sections like the Overview Folders + Topics card grids: every
    // card has a heading, but those cards already live in the left
    // sidebar tree — duplicating them in the right TOC was the
    // single biggest noise source on the Overview page.
    if (el.closest("[data-toc-skip]")) return;

    const text = el.textContent?.trim() || "";
    if (!text) return;

    if (!el.id) {
      el.id = slugify(text);
    }

    items.push({
      id: el.id,
      text,
      level: el.tagName === "H2" ? 2 : 3,
    });
  });

  return items;
}

export function WikiTableOfContents({ contentRef }: WikiTableOfContentsProps) {
  const [items, setItems] = useState<TocItem[]>([]);
  const [activeId, setActiveId] = useState<string>("");
  const observerRef = useRef<IntersectionObserver | null>(null);

  // Extract headings when content changes
  useEffect(() => {
    const container = contentRef.current;
    if (!container) return;

    const extract = () => {
      const headings = extractHeadings(container);
      setItems(headings);
    };

    const timer = setTimeout(extract, 100);

    const mutationObserver = new MutationObserver(() => {
      extract();
    });
    mutationObserver.observe(container, { childList: true, subtree: true });

    return () => {
      clearTimeout(timer);
      mutationObserver.disconnect();
    };
  }, [contentRef]);

  // Track active heading via IntersectionObserver
  useEffect(() => {
    if (items.length === 0) return;

    const scrollContainer = contentRef.current?.closest(".overflow-y-auto") as HTMLElement | null;
    if (!scrollContainer) return;

    observerRef.current?.disconnect();

    const visibleIds = new Set<string>();

    observerRef.current = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            visibleIds.add(entry.target.id);
          } else {
            visibleIds.delete(entry.target.id);
          }
        });

        for (const item of items) {
          if (visibleIds.has(item.id)) {
            setActiveId(item.id);
            return;
          }
        }
      },
      {
        root: scrollContainer,
        rootMargin: "-10% 0px -70% 0px",
        threshold: 0,
      },
    );

    items.forEach(({ id }) => {
      const el = document.getElementById(id);
      if (el) observerRef.current?.observe(el);
    });

    return () => {
      observerRef.current?.disconnect();
    };
  }, [items, contentRef]);

  if (items.length < 2) return null;

  const handleClick = (id: string) => {
    const el = document.getElementById(id);
    if (!el) return;
    const scrollContainer = contentRef.current?.closest(".overflow-y-auto");
    if (scrollContainer) {
      const containerRect = scrollContainer.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      const offset = elRect.top - containerRect.top + scrollContainer.scrollTop - 24;
      scrollContainer.scrollTo({ top: offset, behavior: "smooth" });
    }
    setActiveId(id);
  };

  return (
    <nav>
      <h4 className="text-[11px] font-semibold text-muted-foreground/70 uppercase tracking-widest mb-3">
        On this page
      </h4>
      <div className="relative">
        {/* Continuous left track line */}
        <div className="absolute left-0 top-0 bottom-0 w-px bg-border" />

        <div className="space-y-px">
          {items.map((item) => {
            const isActive = activeId === item.id;
            return (
              <button
                key={item.id}
                onClick={() => handleClick(item.id)}
                className={`relative block w-full text-left text-[12px] leading-relaxed py-1.5 transition-colors duration-150 ${
                  item.level === 3 ? "pl-5" : "pl-3"
                } ${
                  isActive
                    ? "text-primary font-medium"
                    : "text-muted-foreground/60 hover:text-muted-foreground"
                }`}
                title={item.text}
              >
                {/* Active indicator bar */}
                {isActive && (
                  <span className="absolute left-0 top-1 bottom-1 w-0.5 rounded-full bg-primary" />
                )}
                <span className="line-clamp-2">{item.text}</span>
              </button>
            );
          })}
        </div>
      </div>
    </nav>
  );
}
