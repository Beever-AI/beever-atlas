import { type ReactNode, useState, useCallback, useRef, useEffect } from "react";
import { Download, Search, X, ChevronUp, ChevronDown, History, Menu } from "lucide-react";
import { WikiSidebar } from "./WikiSidebar";
import { WikiBreadcrumb } from "./WikiBreadcrumb";
import { FreshnessBadge } from "./FreshnessBadge";
import { WikiTableOfContents } from "./WikiTableOfContents";
import { VersionHistoryPanel } from "./VersionHistoryPanel";
import { WikiRegenerateButton } from "@/components/channel/WikiRegenerateButton";
import type { WikiStructure, WikiPage, WikiVersionSummary } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";
import { authFetch, API_BASE } from "@/lib/api";

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
  /** BCP-47 tag of the language currently displayed. Shown as a chip in the
   *  sidebar header so users can see at a glance which rendering they're viewing. */
  currentLang?: string;
  /** Full list of supported BCP-47 tags for the regenerate language picker. */
  supportedLanguages?: string[];
  /** Called when the user picks a different language from the regenerate menu. */
  onRegenerateInLang?: (lang: string) => void;
}

const MIN_WIDTH = 180;
const MAX_WIDTH = 400;
const DEFAULT_WIDTH = 240;
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
  channelId,
  structure,
  activePage,
  onNavigate,
  onRefresh,
  isRefreshing,
  children,
  versionCount = 0,
  versions = [],
  isVersionsLoading = false,
  viewingVersionNumber = null,
  onSelectVersion,
  onBackToCurrent,
  headerExtra,
  currentLang,
  supportedLanguages,
  onRegenerateInLang,
}: WikiLayoutProps) {
  const [sidebarWidth, setSidebarWidth] = useState(DEFAULT_WIDTH);
  const [showVersionHistory, setShowVersionHistory] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const isDragging = useRef(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const searchableContentRef = useRef<HTMLDivElement>(null);

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
        <div className="p-4 pb-2 shrink-0">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 min-w-0">
              <h3 className="text-sm font-semibold text-foreground truncate">{wikiT(currentLang, "wiki")}</h3>
              {currentLang && (
                <span
                  className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-primary"
                  title={`Displaying wiki in ${currentLang.toUpperCase()}`}
                >
                  {currentLang.toUpperCase()}
                </span>
              )}
            </div>
            {headerExtra}
          </div>
          <FreshnessBadge
            isStale={structure.is_stale}
            generatedAt={structure.generated_at}
            onRefresh={onRefresh}
            isRefreshing={isRefreshing}
            showRefreshButton={false}
            className="mt-2"
            lang={currentLang}
          />
          <div className="mt-2">
            <WikiContentSearch key={activePage.id} contentRef={searchableContentRef} lang={currentLang} />
          </div>
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
        <div className="shrink-0 border-t border-border/70 p-3 space-y-2">
          {/* Primary action: Regenerate (with language picker). */}
          {currentLang && supportedLanguages && onRegenerateInLang && (
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
          )}
          {/* Secondary actions: Download + Version History. */}
          <div className="flex gap-1.5">
            <button
              onClick={async () => {
                try {
                  const res = await authFetch(`${API_BASE}/api/channels/${channelId}/wiki/download`);
                  if (!res.ok) {
                    console.error("[wiki] download failed", res.status);
                    alert(`Download failed (${res.status})`);
                    return;
                  }
                  const blob = await res.blob();
                  const disposition = res.headers.get("Content-Disposition") || "";
                  const nameMatch = disposition.match(/filename[^;=\n]*=["']?([^"';\n]+)/);
                  const filename = nameMatch ? nameMatch[1].trim() : `${channelId}-wiki.md`;
                  const url = URL.createObjectURL(blob);
                  const a = document.createElement("a");
                  a.href = url;
                  a.download = filename;
                  a.click();
                  URL.revokeObjectURL(url);
                } catch (err) {
                  console.error("[wiki] download error", err);
                  alert("Download failed. Please try again.");
                }
              }}
              className="flex items-center justify-center gap-1.5 flex-1 rounded-md px-3 py-1.5 text-xs font-medium border border-border/50 bg-muted/40 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              title="Download as Markdown"
            >
              <Download className="h-3.5 w-3.5" />
              {wikiT(currentLang, "download")}
            </button>
            <button
              onClick={() => setShowVersionHistory(!showVersionHistory)}
              disabled={versionCount === 0}
              className={`flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium border transition-colors ${
                showVersionHistory
                  ? "border-primary/30 bg-primary/10 text-primary"
                  : "border-border/50 bg-muted/40 text-muted-foreground hover:bg-muted hover:text-foreground"
              } disabled:opacity-40 disabled:cursor-not-allowed`}
              title={versionCount === 0 ? "No previous versions" : `${versionCount} previous version${versionCount !== 1 ? "s" : ""}`}
            >
              <History className="h-3.5 w-3.5" />
              {versionCount > 0 && (
                <span className="tabular-nums">{versionCount}</span>
              )}
            </button>
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
            onClose={() => setShowVersionHistory(false)}
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

      {/* Right TOC Sidebar */}
      <div className="hidden xl:block w-48 shrink-0 overflow-y-auto">
        <div className="sticky top-0 px-4 py-8">
          <WikiTableOfContents contentRef={contentRef} />
        </div>
      </div>
    </div>
  );
}
