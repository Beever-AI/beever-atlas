import { type ReactNode, useState, useCallback, useRef, useEffect, useMemo } from "react";
import { Search, X, ChevronUp, ChevronDown, Menu } from "lucide-react";
import { WikiSidebar } from "./WikiSidebar";
import { WikiBreadcrumb } from "./WikiBreadcrumb";
import { FreshnessBadge } from "./FreshnessBadge";
import { WikiTableOfContents } from "./WikiTableOfContents";
import { NarrativeTOC } from "./NarrativeTOC";
import { VersionHistoryPanel } from "./VersionHistoryPanel";
import { WikiRegenerateButton } from "@/components/channel/WikiRegenerateButton";
import type { WikiStructure, WikiPage, WikiVersionSummary } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";

/** Extract narrative-article TOC sections from a wiki page.
 *
 *  The narrative payload lives in TWO places after the
 *  wiki-narrative-articles change: the page-level ``narrative_sections``
 *  field (the canonical source — mirrored from persistence) AND the
 *  ``narrative_article`` module's ``data.sections`` slice (built by
 *  ``build_narrative_article_data`` for the React dispatcher). We
 *  prefer the module-data path because it's been through the section
 *  cleanup that drops anchorless / paragraph-less entries; we fall
 *  back to the page-level field when the module entry is missing
 *  (older docs that persisted ``narrative_sections`` but predate the
 *  ``narrative_article`` planner pick).
 */
function extractNarrativeTocSections(
  page: WikiPage,
): Array<{ anchor: string; heading: string }> {
  const sections: Array<{ anchor: string; heading: string }> = [];
  const seen = new Set<string>();
  const push = (anchor: unknown, heading: unknown) => {
    if (typeof anchor !== "string" || !anchor) return;
    if (typeof heading !== "string" || !heading) return;
    if (seen.has(anchor)) return;
    seen.add(anchor);
    sections.push({ anchor, heading });
  };
  const narrativeModule = (page.modules ?? []).find(
    (m) => m.id === "narrative_article",
  );
  if (narrativeModule?.data) {
    const moduleSections = (narrativeModule.data as { sections?: unknown })
      .sections;
    if (Array.isArray(moduleSections)) {
      for (const s of moduleSections) {
        if (s && typeof s === "object") {
          const r = s as Record<string, unknown>;
          push(r.anchor, r.heading);
        }
      }
    }
  }
  if (sections.length === 0 && Array.isArray(page.narrative_sections)) {
    for (const s of page.narrative_sections) {
      push(s?.anchor, s?.heading);
    }
  }
  return sections;
}

interface WikiLayoutProps {
  channelId: string;
  structure: WikiStructure;
  activePage: WikiPage;
  onNavigate: (pageId: string) => void;
  onRefresh: () => void;
  isRefreshing: boolean;
  children: ReactNode;
  versionCount?: number;
  versions?: WikiVersionSummary[];
  isVersionsLoading?: boolean;
  viewingVersionNumber?: number | null;
  onSelectVersion?: (versionNumber: number) => void;
  onBackToCurrent?: () => void;
  headerExtra?: ReactNode;
  /** Optional primary view toggle (e.g. Pages | Graph). Rendered as a
   *  full-width segmented control on its own row at the top of the
   *  sidebar header so it doesn't compete with utility actions for
   *  horizontal space. */
  viewToggle?: ReactNode;
  /** Optional small accessory rendered next to the WIKI label in the
   *  meta row — typically a "?" info button explaining the toggle
   *  options. Kept separate from `viewToggle` so it doesn't compete
   *  for horizontal space inside the segmented control. */
  headerExplainer?: ReactNode;
  /** BCP-47 tag of the language currently displayed. Shown as a chip in the
   *  sidebar header so users can see at a glance which rendering they're viewing. */
  currentLang?: string;
  /** Full list of supported BCP-47 tags for the regenerate language picker. */
  supportedLanguages?: string[];
  /** Called when the user picks a different language from the regenerate menu. */
  onRegenerateInLang?: (lang: string) => void;
  /**
   * Controlled open state for the version history panel.
   * When provided, the layout uses this value instead of its own internal state.
   */
  versionHistoryOpen?: boolean;
  /** Called when the user requests to toggle the version history panel. */
  onVersionHistoryToggle?: () => void;
}

const MIN_WIDTH = 200;
const MAX_WIDTH = 460;
// Bumped to 320 so multi-level folder trees + 2-line wrapped titles
// have enough horizontal room before the resize handle has to be used.
const DEFAULT_WIDTH = 320;
const SEARCH_MARK_ATTR = "data-wiki-search-mark";

function clearSearchHighlights(root: HTMLElement | null) {
  if (!root) return;
  const marks = root.querySelectorAll(`mark[${SEARCH_MARK_ATTR}="true"]`);
  marks.forEach((mark) => {
    const parent = mark.parentNode;
    if (!parent) return;
    parent.replaceChild(document.createTextNode(mark.textContent || ""), mark);
    parent.normalize();
  });
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function highlightSearchMatches(root: HTMLElement | null, query: string): HTMLElement[] {
  clearSearchHighlights(root);
  if (!root) return [];

  const normalizedQuery = query.trim();
  if (!normalizedQuery) return [];

  const regex = new RegExp(escapeRegExp(normalizedQuery), "gi");
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const text = node.textContent || "";
      if (!text.trim()) return NodeFilter.FILTER_REJECT;
      const parent = node.parentElement;
      if (!parent) return NodeFilter.FILTER_REJECT;
      if (parent.closest("script, style, mark")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });

  const textNodes: Text[] = [];
  let currentNode = walker.nextNode();
  while (currentNode) {
    textNodes.push(currentNode as Text);
    currentNode = walker.nextNode();
  }

  const marks: HTMLElement[] = [];

  textNodes.forEach((textNode) => {
    const text = textNode.textContent || "";
    regex.lastIndex = 0;
    if (!regex.test(text)) return;
    regex.lastIndex = 0;

    const fragment = document.createDocumentFragment();
    let lastIndex = 0;
    let match = regex.exec(text);

    while (match) {
      const matchIndex = match.index;
      const matchText = match[0];

      if (matchIndex > lastIndex) {
        fragment.appendChild(document.createTextNode(text.slice(lastIndex, matchIndex)));
      }

      const mark = document.createElement("mark");
      mark.setAttribute(SEARCH_MARK_ATTR, "true");
      mark.className = "rounded bg-amber-200/70 px-0.5 text-foreground";
      mark.textContent = matchText;
      fragment.appendChild(mark);
      marks.push(mark);

      lastIndex = matchIndex + matchText.length;
      match = regex.exec(text);
    }

    if (lastIndex < text.length) {
      fragment.appendChild(document.createTextNode(text.slice(lastIndex)));
    }

    const parent = textNode.parentNode;
    if (parent) {
      parent.replaceChild(fragment, textNode);
    }
  });

  return marks;
}

function setActiveSearchMatch(marks: HTMLElement[], activeIndex: number) {
  marks.forEach((mark, index) => {
    if (index === activeIndex) {
      mark.className = "rounded bg-amber-400 px-0.5 text-foreground";
    } else {
      mark.className = "rounded bg-amber-200/70 px-0.5 text-foreground";
    }
  });
}

interface WikiContentSearchProps {
  contentRef: React.RefObject<HTMLDivElement | null>;
  lang?: string;
}

function WikiContentSearch({ contentRef, lang }: WikiContentSearchProps) {
  const [query, setQuery] = useState("");
  const [matchCount, setMatchCount] = useState(0);
  const [activeMatchIndex, setActiveMatchIndex] = useState(-1);
  const marksRef = useRef<HTMLElement[]>([]);

  const runSearch = useCallback(
    (nextQuery: string) => {
      const marks = highlightSearchMatches(contentRef.current, nextQuery);
      marksRef.current = marks;
      setMatchCount(marks.length);
      if (marks.length === 0) {
        setActiveMatchIndex(-1);
        return;
      }
      setActiveMatchIndex(0);
      setActiveSearchMatch(marks, 0);
      marks[0].scrollIntoView({ block: "center", behavior: "smooth" });
    },
    [contentRef],
  );

  const clearSearch = useCallback(() => {
    clearSearchHighlights(contentRef.current);
    marksRef.current = [];
    setQuery("");
    setMatchCount(0);
    setActiveMatchIndex(-1);
  }, [contentRef]);

  const moveToMatch = useCallback(
    (direction: -1 | 1) => {
      const marks = marksRef.current;
      if (marks.length === 0 || activeMatchIndex < 0) return;
      const nextIndex = (activeMatchIndex + direction + marks.length) % marks.length;
      setActiveMatchIndex(nextIndex);
      setActiveSearchMatch(marks, nextIndex);
      marks[nextIndex].scrollIntoView({ block: "center", behavior: "smooth" });
    },
    [activeMatchIndex],
  );

  useEffect(() => () => clearSearchHighlights(contentRef.current), [contentRef]);

  return (
    <div className="relative">
      <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground/60 pointer-events-none" />
      <input
        type="text"
        value={query}
        onChange={(e) => {
          const nextQuery = e.target.value;
          setQuery(nextQuery);
          runSearch(nextQuery);
        }}
        placeholder={wikiT(lang, "searchPlaceholder") + "..."}
        className="w-full rounded-lg border border-border/40 bg-muted/60 py-1.5 pl-8 pr-20 text-[13px] text-foreground placeholder:text-muted-foreground/50 focus:bg-muted/80 focus:border-primary/40 focus:outline-none focus:ring-1 focus:ring-primary/20 transition-all"
        aria-label="Search current wiki page"
      />
      {query && (
        <div className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1">
          <span className="text-[11px] text-muted-foreground tabular-nums min-w-8 text-right">
            {matchCount > 0 ? `${activeMatchIndex + 1}/${matchCount}` : "0/0"}
          </span>
          <button
            onClick={() => moveToMatch(-1)}
            disabled={matchCount === 0}
            className="rounded p-0.5 text-muted-foreground hover:bg-muted-foreground/10 hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
            aria-label="Previous match"
          >
            <ChevronUp className="h-3 w-3" />
          </button>
          <button
            onClick={() => moveToMatch(1)}
            disabled={matchCount === 0}
            className="rounded p-0.5 text-muted-foreground hover:bg-muted-foreground/10 hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
            aria-label="Next match"
          >
            <ChevronDown className="h-3 w-3" />
          </button>
          <button
            onClick={clearSearch}
            className="rounded p-0.5 text-muted-foreground hover:bg-muted-foreground/10 hover:text-foreground"
            aria-label="Clear search"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      )}
    </div>
  );
}

export function WikiLayout({
  // ``channelId`` and ``versionCount`` remain in ``WikiLayoutProps`` for
  // callers (the parent passes them and we want a stable prop API), but
  // the layout no longer uses them directly — Download / History moved
  // out of the sidebar footer into the Tools dropdown owned by
  // ``WikiHealthToolbar`` (commit that simplified the wiki UX).
  structure,
  activePage,
  onNavigate,
  onRefresh,
  isRefreshing,
  children,
  versions = [],
  isVersionsLoading = false,
  viewingVersionNumber = null,
  onSelectVersion,
  onBackToCurrent,
  headerExtra,
  viewToggle,
  headerExplainer,
  currentLang,
  supportedLanguages,
  onRegenerateInLang,
  versionHistoryOpen,
  onVersionHistoryToggle,
}: WikiLayoutProps) {
  const [sidebarWidth, setSidebarWidth] = useState(DEFAULT_WIDTH);
  const [internalVersionHistoryOpen, setInternalVersionHistoryOpen] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  // Support both controlled (versionHistoryOpen prop) and uncontrolled mode
  const showVersionHistory = versionHistoryOpen !== undefined ? versionHistoryOpen : internalVersionHistoryOpen;
  const handleVersionHistoryToggle = onVersionHistoryToggle ?? (() => setInternalVersionHistoryOpen((v) => !v));
  const isDragging = useRef(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const searchableContentRef = useRef<HTMLDivElement>(null);

  // Narrative TOC sections come from the active page's narrative
  // payload (page-level field or ``narrative_article`` module data).
  // When ≥3 sections exist the sticky TOC mounts in the right rail
  // and ``WikiTableOfContents`` is suppressed (the article body has
  // ``data-toc-skip`` so the existing TOC wouldn't list narrative
  // headings anyway, but suppressing the legacy fallback keeps the
  // rail focused on the article structure).
  const narrativeTocSections = useMemo(
    () => extractNarrativeTocSections(activePage),
    [activePage],
  );
  const hasNarrativeToc = narrativeTocSections.length >= 3;

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isDragging.current = true;
    const startX = e.clientX;
    const startWidth = sidebarWidth;

    const onMouseMove = (moveEvent: MouseEvent) => {
      if (!isDragging.current) return;
      const delta = moveEvent.clientX - startX;
      const newWidth = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, startWidth + delta));
      setSidebarWidth(newWidth);
    };

    const onMouseUp = () => {
      isDragging.current = false;
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  }, [sidebarWidth]);

  return (
    <div className="flex h-full relative">
      {/* Mobile sidebar backdrop */}
      {mobileSidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          onClick={() => setMobileSidebarOpen(false)}
        />
      )}
      {/* Left Sidebar */}
      <div
        className={`border-r border-border bg-background flex flex-col min-h-0 transition-transform duration-200 fixed inset-y-0 left-0 z-40 !w-[85vw] max-w-[320px] md:!w-[var(--wiki-sidebar-w)] md:static md:shrink-0 md:z-auto md:max-w-none md:translate-x-0 ${
          mobileSidebarOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0"
        }`}
        style={{ ["--wiki-sidebar-w" as string]: `${sidebarWidth}px` }}
      >
        <div className="px-3 pt-3 pb-2 shrink-0 space-y-2">
          {/* Row 1 — primary view toggle (Pages | Graph) goes full-width
              at the top so it reads as the main sub-navigation. */}
          {viewToggle && (
            <div className="[&>div]:!w-full [&_button]:!flex-1 [&_button]:!justify-center">
              {viewToggle}
            </div>
          )}

          {/* Row 2 — meta only: small WIKI label + lang chip + freshness
              pill. No action buttons on this row — they were overflowing
              the 270px sidebar when packed alongside the meta. */}
          <div className="flex items-center gap-1.5 min-w-0">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground/65 shrink-0">
              {wikiT(currentLang, "wiki")}
            </h3>
            {headerExplainer}
            {currentLang && (
              <span
                className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-primary"
                title={`Displaying wiki in ${currentLang.toUpperCase()}`}
              >
                {currentLang.toUpperCase()}
              </span>
            )}
            <FreshnessBadge
              isStale={structure.is_stale}
              generatedAt={structure.generated_at}
              onRefresh={onRefresh}
              isRefreshing={isRefreshing}
              showRefreshButton={false}
              className="!text-[10px] !px-1.5 !py-0.5 ml-auto"
              lang={currentLang}
            />
          </div>

          {/* Row 3 — page search. The action toolbar (Tools dropdown)
              moved to the sidebar footer so the header is now purely
              nav + meta + search; actions live alongside Regenerate at
              the bottom where the eye expects primary CTAs. */}
          <WikiContentSearch
            key={activePage.id}
            contentRef={searchableContentRef}
            lang={currentLang}
          />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <WikiSidebar
            pages={structure.pages}
            activePageId={activePage.id}
            onNavigate={(id) => {
              onNavigate(id);
              setMobileSidebarOpen(false);
            }}
            lang={currentLang}
          />
        </div>
        <div className="shrink-0 border-t border-border/70 p-3">
          {/* Footer action zone:
              - Regenerate (primary, flexes to fill)
              - Tools (secondary, square icon button via headerExtra)
              Tooltips inside the toolbar are clamped so they don't
              bleed into the main content area to the right. */}
          <div className="flex items-stretch gap-2 [&_[role=tooltip]]:!max-w-[200px]">
            {currentLang && supportedLanguages && onRegenerateInLang && (
              <div className="flex-1 min-w-0">
                <WikiRegenerateButton
                  currentLang={currentLang}
                  supportedLanguages={supportedLanguages}
                  isRefreshing={isRefreshing}
                  onRegenerate={onRefresh}
                  onRegenerateInLang={onRegenerateInLang}
                  size="md"
                  fullWidth
                  lang={currentLang}
                />
              </div>
            )}
            {headerExtra && (
              <div className="shrink-0 flex items-center">
                {headerExtra}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Version History Panel */}
      {showVersionHistory && (
        <div className="w-[240px] shrink-0 border-r border-border bg-background">
          <VersionHistoryPanel
            versions={versions}
            isLoading={isVersionsLoading}
            activeVersionNumber={viewingVersionNumber}
            onSelectVersion={(v) => {
              onSelectVersion?.(v);
            }}
            onBackToCurrent={() => {
              onBackToCurrent?.();
            }}
            onClose={handleVersionHistoryToggle}
            lang={currentLang}
          />
        </div>
      )}

      {/* Resize handle — desktop only */}
      <div
        onMouseDown={handleMouseDown}
        className="hidden md:block w-1 shrink-0 cursor-col-resize hover:bg-primary/20 active:bg-primary/30 transition-colors"
      />

      {/* Main Content */}
      <div className="flex-1 overflow-y-auto min-w-0">
        <div className="max-w-4xl mx-auto px-4 sm:px-6 md:px-8 py-4 md:py-6" ref={contentRef}>
          <div className="flex items-center gap-2 md:hidden mb-2">
            <button
              onClick={() => setMobileSidebarOpen(true)}
              className="inline-flex items-center justify-center size-9 rounded-md border border-border bg-background text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
              aria-label="Open wiki navigation"
            >
              <Menu className="h-4 w-4" />
            </button>
          </div>
          <WikiBreadcrumb page={activePage} />
          <div ref={searchableContentRef}>
            {children}
          </div>
        </div>
      </div>

      {/* Right TOC Sidebar — when the active page has a multi-section
          narrative article (≥3 sections), mount ``NarrativeTOC`` so
          readers get a sticky table of contents tied to the article's
          own anchors. The narrative article body is wrapped in
          ``data-toc-skip`` so ``WikiTableOfContents`` already ignores
          its headings; we still swap the rail entirely for narrative
          pages because the heading-extraction TOC would be empty
          anyway, and the narrative TOC carries semantically richer
          labels (the planner's section headings, not raw H2 text). */}
      <div className="hidden xl:block w-48 shrink-0 overflow-y-auto">
        <div className="sticky top-0 px-4 py-8">
          {hasNarrativeToc ? (
            <NarrativeTOC sections={narrativeTocSections} />
          ) : (
            <WikiTableOfContents contentRef={contentRef} />
          )}
        </div>
      </div>
    </div>
  );
}
