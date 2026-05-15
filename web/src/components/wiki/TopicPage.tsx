import { useMemo } from "react";
import { ChevronRight, Clock, FileText, Quote } from "lucide-react";
import { WikiMarkdown } from "./WikiMarkdown";
import { CitationPanel } from "./CitationPanel";
import { TensionsSection, type WikiTension } from "./TensionsSection";
import { ModuleRenderer } from "./modules/ModuleRenderer";
import { ContributorGrid } from "./ContributorCard";
import { ToolGrid } from "./ToolCard";
import { parseOverviewBody } from "@/lib/overviewSections";
import type { WikiPage } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";

// Quick relative-time formatter — same shape as the one in
// OverviewPage / FolderPage so chips read consistently across page
// types. Avoids pulling a heavy date-fns dep just for "2h ago".
function relativeAgo(iso: string | undefined | null): string {
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

interface ChipProps {
  icon: React.ReactNode;
  label: string;
  value: number | string;
}
function MetaChip({ icon, label, value }: ChipProps) {
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

interface TopicPageProps {
  page: WikiPage & { tensions?: WikiTension[] };
  onNavigate: (pageId: string) => void;
  lang?: string;
}

export function TopicPage({ page, onNavigate, lang }: TopicPageProps) {
  const content = page.content.replace(/^#\s+[^\n]+\n*/, "");
  const isSubTopic = page.page_type === "sub-topic" && page.parent_id;
  const hasChildren = page.children && page.children.length > 0;

  return (
    <div>
      {/* Breadcrumb for sub-topic pages */}
      {isSubTopic && (
        <nav className="flex items-center gap-1 text-sm text-muted-foreground mb-2">
          <button
            onClick={() => onNavigate(page.parent_id!)}
            className="hover:text-foreground hover:underline transition-colors"
          >
            {page.parent_id!.replace("topic-", "").replace(/-/g, " ")}
          </button>
          <ChevronRight className="h-3 w-3 shrink-0" />
          <span className="text-foreground font-medium">{page.title}</span>
        </nav>
      )}

      <h1 className="text-2xl font-bold text-foreground">{page.title}</h1>
      {/* Header chip row — matches the OverviewPage / FolderPage chip
          shape so the eye learns one pattern across page types.
          Optional chips collapse when their data is missing rather
          than showing zero or empty placeholders. */}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <MetaChip
          icon={<FileText size={11} />}
          label={page.memory_count === 1 ? "memory" : "memories"}
          value={page.memory_count}
        />
        {page.citations && page.citations.length > 0 && (
          <MetaChip
            icon={<Quote size={11} />}
            label={page.citations.length === 1 ? "citation" : "citations"}
            value={page.citations.length}
          />
        )}
        {relativeAgo(page.last_updated) && (
          <MetaChip
            icon={<Clock size={11} />}
            label={relativeAgo(page.last_updated)}
            value="updated"
          />
        )}
      </div>

      {/* Table of contents for parent pages with sub-pages */}
      {hasChildren && (
        <div className="mt-4 rounded-lg border border-border/60 bg-muted/20 p-4">
          <h3 className="text-sm font-semibold text-foreground mb-2">{wikiT(lang, "subTopics")}</h3>
          <ul className="space-y-1">
            {page.children.map((child) => (
              <li key={child.id}>
                <button
                  onClick={() => onNavigate(child.id)}
                  className="text-sm text-primary hover:underline"
                >
                  {child.section_number && (
                    <span className="text-xs text-muted-foreground font-mono mr-1.5">{child.section_number}</span>
                  )}
                  {child.title}
                  {child.memory_count > 0 && (
                    <span className="ml-1.5 text-xs text-muted-foreground">
                      ({wikiT(lang, "memoriesSuffix", { n: child.memory_count })})
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Module-aware rendering when the planner produced a modules
          plan; falls back to the legacy single-markdown-blob render
          for pages persisted before the adaptive-modules system. The
          two paths produce equivalent output today (the orchestrator
          ALSO substitutes module markers into ``content`` for legacy
          fallback); the dispatcher path is what Phase 7+ swaps in
          rich media renderers. */}
      {page.modules && page.modules.length > 0 ? (
        <div className="max-w-none">
          <ModuleRenderer
            modules={page.modules}
            citations={page.citations}
            onNavigate={onNavigate}
          />
        </div>
      ) : (
        <LegacyTopicBody
          content={content}
          citations={page.citations}
          onNavigate={onNavigate}
        />
      )}

      {/* Inline contradictions detected between facts on this page.
          Wrapped in ``data-toc-skip`` so the contradictions section
          headings (when present) don't pollute the right TOC across
          pages with different module mixes. */}
      <div data-toc-skip>
        <TensionsSection tensions={page.tensions} />
      </div>

      {/* CitationPanel is wrapped in ``data-toc-skip`` for the same
          reason — it's a fixed-shell appendix, not part of the
          navigable body. */}
      <div data-toc-skip>
        <CitationPanel citations={page.citations} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// LegacyTopicBody — renders pre-modules topic content. Parses the
// LLM-emitted body into structured sections (Contributors → cards,
// Tools & resources → cards) so the legacy path benefits from the
// same visual treatment the Overview got. Anything the parser
// doesn't recognise falls through to WikiMarkdown.
// ---------------------------------------------------------------------------

interface LegacyTopicBodyProps {
  content: string;
  citations: WikiPage["citations"];
  onNavigate: (pageId: string) => void;
}

function LegacyTopicBody({ content, citations, onNavigate }: LegacyTopicBodyProps) {
  const parsed = useMemo(() => parseOverviewBody(content), [content]);
  return (
    <div className="mt-6 max-w-none">
      {parsed.tldr && (
        <p className="text-base font-semibold text-foreground leading-relaxed border-l-2 border-primary/60 pl-3 mb-3">
          {parsed.tldr}
        </p>
      )}
      {parsed.intro && (
        <WikiMarkdown
          content={parsed.intro}
          citations={citations}
          onNavigate={onNavigate}
        />
      )}
      {parsed.residualBody && (
        <WikiMarkdown
          content={parsed.residualBody}
          citations={citations}
          onNavigate={onNavigate}
        />
      )}
      {/* Concept Diagram (mermaid) — show in place rather than
          collapsed for topic pages (less aggressive than Overview's
          collapsed-by-default treatment because topic-page diagrams
          are typically smaller and more focused). */}
      {parsed.conceptMapMermaid && (
        <div className="mt-6">
          <WikiMarkdown
            content={"```mermaid\n" + parsed.conceptMapMermaid + "\n```"}
            citations={citations}
            onNavigate={onNavigate}
          />
        </div>
      )}
      {parsed.contributors.length > 0 && (
        <ContributorGrid groups={parsed.contributors} title="Contributors" />
      )}
      {parsed.tools.length > 0 && <ToolGrid tools={parsed.tools} />}
    </div>
  );
}
