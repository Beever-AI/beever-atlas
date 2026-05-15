import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Clock, FileText, Folder, FolderOpen } from "lucide-react";
import { WikiMarkdown } from "./WikiMarkdown";
import { TopicCard } from "./TopicCard";
import { CitationPanel } from "./CitationPanel";
import { ContributorGrid } from "./ContributorCard";
import { ToolGrid } from "./ToolCard";
import type { WikiPage, WikiPageNode } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";
import { parseOverviewBody } from "@/lib/overviewSections";

interface OverviewPageProps {
  page: WikiPage;
  topicPages: WikiPageNode[];
  /** Folder pages (planner-produced groupings). When empty, the
   *  Folders section is hidden and the topic cards represent the
   *  full topic list. */
  folderPages?: WikiPageNode[];
  /** ISO timestamp the wiki was last regenerated. Surfaced as a
   *  freshness chip in the header. */
  generatedAt?: string;
  onNavigate: (pageId: string) => void;
  lang?: string;
}

// ---------------------------------------------------------------------------
// Quick relative-time formatter (shared shape with FolderPage / TopicPage).
// ---------------------------------------------------------------------------

function relativeAgo(iso: string | undefined): string {
  if (!iso) return "";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "";
  const diffMs = Date.now() - ts;
  const min = Math.round(diffMs / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day}d ago`;
  const mon = Math.round(day / 30);
  return `${mon}mo ago`;
}

interface MetaChipProps {
  icon: React.ReactNode;
  label: string;
  value: number | string;
}
function MetaChip({ icon, label, value }: MetaChipProps) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-muted/40 px-2 py-1 text-[11px] text-muted-foreground"
      title={label}
    >
      <span className="text-muted-foreground/70">{icon}</span>
      <span className="font-semibold text-foreground tabular-nums">{value}</span>
      <span>{label}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Folder card — recursive child preview list. Same shape as before.
// ---------------------------------------------------------------------------

interface FolderCardProps {
  folder: WikiPageNode;
  onNavigate: (pageId: string) => void;
  lang?: string;
}
function FolderCard({ folder, onNavigate, lang }: FolderCardProps) {
  const flatten = (node: WikiPageNode, acc: WikiPageNode[]): WikiPageNode[] => {
    for (const child of node.children) {
      if (child.page_type === "folder") {
        flatten(child, acc);
      } else {
        acc.push(child);
      }
    }
    return acc;
  };
  const allLeaves = flatten(folder, []);
  const previewLeaves = allLeaves.slice(0, 5);
  const overflow = Math.max(0, allLeaves.length - previewLeaves.length);
  const totalMemories = allLeaves.reduce((s, p) => s + (p.memory_count ?? 0), 0);

  return (
    <div className="rounded-xl border border-border bg-card p-4 hover:border-primary/30 transition-colors">
      <button
        type="button"
        onClick={() => onNavigate(folder.id)}
        className="flex items-start gap-2.5 text-left w-full mb-3 group"
      >
        <span className="shrink-0 flex h-7 w-7 items-center justify-center rounded-md bg-primary/10 text-primary group-hover:bg-primary/15">
          <FolderOpen size={14} />
        </span>
        <span className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-foreground leading-snug group-hover:text-primary transition-colors">
            {folder.title}
          </h3>
          <p className="mt-0.5 text-[11px] text-muted-foreground tabular-nums">
            {allLeaves.length} {allLeaves.length === 1 ? "page" : "pages"}
            {totalMemories > 0 && ` · ${wikiT(lang, "memoriesSuffix", { n: totalMemories })}`}
          </p>
        </span>
      </button>
      {previewLeaves.length > 0 && (
        <ul className="space-y-1 border-t border-border/60 pt-2.5">
          {previewLeaves.map((leaf) => (
            <li key={leaf.id}>
              <button
                type="button"
                onClick={() => onNavigate(leaf.id)}
                className="flex items-start gap-1.5 w-full text-left text-[12px] text-muted-foreground hover:text-foreground rounded-sm px-1 py-0.5 hover:bg-muted/60 transition-colors"
              >
                <FileText size={11} className="shrink-0 mt-0.5 text-muted-foreground/50" />
                <span className="line-clamp-1 flex-1">{leaf.title}</span>
                {leaf.memory_count > 0 && (
                  <span className="shrink-0 text-[10.5px] text-muted-foreground/55 tabular-nums">
                    {leaf.memory_count}
                  </span>
                )}
              </button>
            </li>
          ))}
          {overflow > 0 && (
            <li className="px-1 pt-0.5 text-[11px] text-muted-foreground/60 italic">
              +{overflow} more page{overflow === 1 ? "" : "s"}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// OverviewPage
// ---------------------------------------------------------------------------

export function OverviewPage({
  page,
  topicPages,
  folderPages = [],
  generatedAt,
  onNavigate,
  lang,
}: OverviewPageProps) {
  // Strip leading h1 from LLM content to avoid duplicate title.
  const rawContent = page.content.replace(/^#\s+[^\n]+\n*/, "");
  // Parse the LLM body into structured sections. Anything we don't
  // recognise stays in ``residualBody`` for WikiMarkdown to render.
  const parsed = useMemo(() => parseOverviewBody(rawContent), [rawContent]);

  const totalTopics = useMemo(() => {
    const countLeaves = (n: WikiPageNode): number =>
      n.page_type === "folder"
        ? n.children.reduce((s, c) => s + countLeaves(c), 0)
        : 1;
    const inFolders = folderPages.reduce((s, f) => s + countLeaves(f), 0);
    return inFolders + topicPages.length;
  }, [folderPages, topicPages.length]);

  const [conceptOpen, setConceptOpen] = useState(false);
  // Topic grid collapses to a manageable preview when there are
  // many topics. Click "Show all" to expand the full catalog. The
  // PREVIEW_LIMIT (9 = 3 rows × 3 columns at the lg breakpoint)
  // matches the visible-fold rule on a typical desktop monitor.
  const TOPIC_PREVIEW_LIMIT = 9;
  const [topicsExpanded, setTopicsExpanded] = useState(false);
  const freshness = relativeAgo(generatedAt);

  return (
    <div>
      <h1 className="text-2xl font-bold text-foreground">{page.title}</h1>

      {/* ── Header chip row — at-a-glance counts + freshness ───────── */}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <MetaChip
          icon={<FileText size={11} />}
          label={page.memory_count === 1 ? "memory" : "memories"}
          value={page.memory_count}
        />
        {folderPages.length > 0 && (
          <MetaChip icon={<Folder size={11} />} label="folders" value={folderPages.length} />
        )}
        <MetaChip icon={<FileText size={11} />} label="topics" value={totalTopics} />
        {freshness && (
          <MetaChip icon={<Clock size={11} />} label={freshness} value="updated" />
        )}
      </div>

      {/* ── TL;DR — promoted to its own block at the top ───────────── */}
      {parsed.tldr && (
        <p className="mt-5 text-base font-semibold text-foreground leading-relaxed border-l-2 border-primary/60 pl-3">
          {parsed.tldr}
        </p>
      )}

      {/* ── Intro paragraph (whatever remained after TL;DR extract) ── */}
      {parsed.intro && (
        <div className="mt-3 max-w-none">
          <WikiMarkdown content={parsed.intro} citations={page.citations} onNavigate={onNavigate} />
        </div>
      )}

      {/* ── Concept Map (collapsible, prominent placement) ────────────
          Placed right after the intro so it's discoverable above the
          fold without dominating it. Collapsed-by-default keeps the
          page scannable; one click expands the diagram for users
          who want the visual shape of the channel. */}
      {parsed.conceptMapMermaid && (
        <div className="mt-6 rounded-xl border border-border/60 bg-muted/20" data-toc-skip>
          <button
            type="button"
            onClick={() => setConceptOpen((v) => !v)}
            className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-muted/40 transition-colors rounded-xl"
            aria-expanded={conceptOpen}
          >
            <span className="flex items-center gap-2">
              {conceptOpen ? (
                <ChevronDown className="h-4 w-4 text-muted-foreground" />
              ) : (
                <ChevronRight className="h-4 w-4 text-muted-foreground" />
              )}
              <h2 className="text-base font-semibold text-foreground">Concept Map</h2>
            </span>
            <span className="text-[11px] text-muted-foreground/70">
              {conceptOpen ? "Hide" : "Show diagram"}
            </span>
          </button>
          {conceptOpen && (
            <div className="px-4 pb-4">
              <WikiMarkdown
                content={"```mermaid\n" + parsed.conceptMapMermaid + "\n```"}
                citations={page.citations}
                onNavigate={onNavigate}
              />
            </div>
          )}
        </div>
      )}

      {/* ── Folders section — first-class navigation, top of fold ──── */}
      {folderPages.length > 0 && (
        <div className="mt-8" data-toc-skip>
          <h2 className="text-lg font-semibold text-foreground mb-1 flex items-center gap-2">
            <FolderOpen size={16} className="text-primary/80" />
            Folders
            <span className="text-[11px] font-normal text-muted-foreground">
              ({folderPages.length})
            </span>
          </h2>
          <p className="text-[12px] text-muted-foreground mb-4">
            Topics grouped by the structure planner. Click a folder to open its index.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {folderPages.map((folder) => (
              <FolderCard
                key={folder.id}
                folder={folder}
                onNavigate={onNavigate}
                lang={lang}
              />
            ))}
          </div>
        </div>
      )}

      {/* ── All Topics — flattened across folders, the canonical
          searchable card grid. Folder cards above provide grouped
          navigation; this section is the full topic catalog.
          Collapsed-preview when there are many topics so the page
          stays scannable; "Show all" expands to the full catalog. ── */}
      {topicPages.length > 0 && (
        <div className="mt-8" data-toc-skip>
          <h2 className="text-lg font-semibold text-foreground mb-1">
            All Topics
            <span className="ml-2 text-[11px] font-normal text-muted-foreground">
              ({topicPages.length})
            </span>
          </h2>
          <p className="text-[12px] text-muted-foreground mb-4">
            Every topic in the channel — including those nested inside folders. Click a card to open it.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {(topicsExpanded
              ? topicPages
              : topicPages.slice(0, TOPIC_PREVIEW_LIMIT)
            ).map((topic) => (
              <TopicCard key={topic.id} topic={topic} onClick={() => onNavigate(topic.id)} lang={lang} />
            ))}
          </div>
          {topicPages.length > TOPIC_PREVIEW_LIMIT && (
            <div className="mt-4 flex justify-center">
              <button
                type="button"
                onClick={() => setTopicsExpanded((v) => !v)}
                className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-card px-3 py-1.5 text-[12px] font-medium text-muted-foreground hover:border-primary/40 hover:text-foreground transition-colors"
                aria-expanded={topicsExpanded}
              >
                {topicsExpanded ? (
                  <>
                    <ChevronDown className="h-3 w-3 rotate-180" />
                    Show less
                  </>
                ) : (
                  <>
                    <ChevronRight className="h-3 w-3 rotate-90" />
                    Show all {topicPages.length} topics
                  </>
                )}
              </button>
            </div>
          )}
        </div>
      )}

      {/* ── Key Contributors — card grid, parsed from LLM body ─────── */}
      {parsed.contributors.length > 0 && (
        <ContributorGrid groups={parsed.contributors} title="Key Contributors" />
      )}

      {/* ── Tools & Resources — card grid, parsed from LLM body ────── */}
      {parsed.tools.length > 0 && <ToolGrid tools={parsed.tools} />}

      {/* ── Residual LLM body (sections we didn't recognise) ───────── */}
      {parsed.residualBody && (
        <div className="mt-8 max-w-none">
          <WikiMarkdown
            content={parsed.residualBody}
            citations={page.citations}
            onNavigate={onNavigate}
          />
        </div>
      )}

      <CitationPanel citations={page.citations} />
    </div>
  );
}
