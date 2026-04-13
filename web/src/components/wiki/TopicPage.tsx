import { ChevronRight } from "lucide-react";
import { WikiMarkdown } from "./WikiMarkdown";
import { CitationPanel } from "./CitationPanel";
import type { WikiPage } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";

interface TopicPageProps {
  page: WikiPage;
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
      <p className="mt-1 text-sm text-muted-foreground">
        {wikiT(lang, "memoriesSuffix", { n: page.memory_count })}
      </p>

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

      <div className="mt-6 max-w-none">
        <WikiMarkdown content={content} citations={page.citations} onNavigate={onNavigate} />
      </div>

      <CitationPanel citations={page.citations} />
    </div>
  );
}
