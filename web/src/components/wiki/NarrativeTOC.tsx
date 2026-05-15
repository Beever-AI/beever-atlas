/** Sticky table of contents for narrative articles.
 *
 *  Renders only when:
 *    - viewport width >= 1024px (lg breakpoint)
 *    - article has >= 3 sections
 *
 *  Otherwise renders a compact "Jump to section" dropdown so users on
 *  narrow viewports still get a section-level navigation affordance.
 *
 *  Wide viewport: a vertical list with a continuous left track line,
 *  active-section highlighted via IntersectionObserver. Narrow
 *  viewport: a native ``<select>`` element for accessibility — change
 *  events scroll-snap to the chosen anchor.
 */

import { useEffect, useRef, useState } from "react";

interface TocSection {
  anchor: string;
  heading: string;
}

interface NarrativeTOCProps {
  sections: TocSection[];
}

/** Smooth-scroll to a section anchor by id. */
function scrollToAnchor(anchor: string) {
  const el = document.getElementById(anchor);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

export function NarrativeTOC({ sections }: NarrativeTOCProps) {
  const [activeAnchor, setActiveAnchor] = useState<string>("");
  const observerRef = useRef<IntersectionObserver | null>(null);

  // IntersectionObserver to track which section is currently in view —
  // optional polish; if it errors out (jsdom, older browsers) the TOC
  // still works without highlighting.
  useEffect(() => {
    if (sections.length === 0) return;
    if (typeof IntersectionObserver === "undefined") return;

    observerRef.current?.disconnect();
    const visibleAnchors = new Set<string>();
    observerRef.current = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const anchor = entry.target.id;
          if (entry.isIntersecting) {
            visibleAnchors.add(anchor);
          } else {
            visibleAnchors.delete(anchor);
          }
        }
        for (const section of sections) {
          if (visibleAnchors.has(section.anchor)) {
            setActiveAnchor(section.anchor);
            return;
          }
        }
      },
      { rootMargin: "-10% 0px -70% 0px", threshold: 0 },
    );
    for (const section of sections) {
      const el = document.getElementById(section.anchor);
      if (el) observerRef.current.observe(el);
    }
    return () => {
      observerRef.current?.disconnect();
    };
  }, [sections]);

  // Hide entirely when the article is too short for a TOC to add value
  // (the article still works fine without it).
  if (sections.length < 3) return null;

  const handleStickyClick = (e: React.MouseEvent<HTMLAnchorElement>, anchor: string) => {
    e.preventDefault();
    scrollToAnchor(anchor);
    setActiveAnchor(anchor);
  };

  const handleSelectChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const anchor = e.target.value;
    if (!anchor) return;
    scrollToAnchor(anchor);
    setActiveAnchor(anchor);
  };

  return (
    <>
      {/* Sticky panel — wide viewports only (≥1024px) */}
      <nav
        data-testid="narrative-toc-sticky"
        className="hidden lg:block sticky top-20 self-start max-w-xs"
        aria-label="Article sections"
      >
        <h4 className="text-[11px] font-semibold text-muted-foreground/70 uppercase tracking-widest mb-3">
          On this page
        </h4>
        <div className="relative">
          <div
            aria-hidden="true"
            className="absolute left-0 top-0 bottom-0 w-px bg-border"
          />
          <ul className="space-y-px">
            {sections.map((section) => {
              const isActive = activeAnchor === section.anchor;
              return (
                <li key={section.anchor}>
                  <a
                    href={`#${section.anchor}`}
                    data-testid="narrative-toc-link"
                    onClick={(e) => handleStickyClick(e, section.anchor)}
                    className={`relative block w-full text-left text-[12px] leading-relaxed py-1.5 pl-3 transition-colors duration-150 ${
                      isActive
                        ? "text-primary font-medium"
                        : "text-muted-foreground/70 hover:text-foreground"
                    }`}
                    title={section.heading}
                  >
                    {isActive && (
                      <span
                        aria-hidden="true"
                        className="absolute left-0 top-1 bottom-1 w-0.5 rounded-full bg-primary"
                      />
                    )}
                    <span className="line-clamp-2">{section.heading}</span>
                  </a>
                </li>
              );
            })}
          </ul>
        </div>
      </nav>

      {/* Compact dropdown — narrow viewports only (<1024px) */}
      <div
        data-testid="narrative-toc-dropdown"
        className="lg:hidden mb-6"
      >
        <label
          htmlFor="narrative-toc-select"
          className="block text-[11px] font-semibold text-muted-foreground/70 uppercase tracking-widest mb-2"
        >
          Jump to section
        </label>
        <select
          id="narrative-toc-select"
          data-testid="narrative-toc-select"
          className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/30"
          value={activeAnchor}
          onChange={handleSelectChange}
        >
          <option value="">Select a section…</option>
          {sections.map((section) => (
            <option key={section.anchor} value={section.anchor}>
              {section.heading}
            </option>
          ))}
        </select>
      </div>
    </>
  );
}
