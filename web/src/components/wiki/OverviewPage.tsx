import { WikiMarkdown } from "./WikiMarkdown";
import { TopicCard } from "./TopicCard";
import { CitationPanel } from "./CitationPanel";
import type { WikiPage, WikiPageNode } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";

interface OverviewPageProps {
  page: WikiPage;
  topicPages: WikiPageNode[];
  onNavigate: (pageId: string) => void;
  lang?: string;
}

export function OverviewPage({ page, topicPages, onNavigate, lang }: OverviewPageProps) {
  // Strip leading h1 from LLM content to avoid duplicate title
  const content = page.content.replace(/^#\s+[^\n]+\n*/, "");

  return (
    <div>
      <h1 className="text-2xl font-bold text-foreground">{page.title}</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        {wikiT(lang, "memoriesSuffix", { n: page.memory_count })}
      </p>

      <div className="mt-6 max-w-none">
        <WikiMarkdown content={content} citations={page.citations} onNavigate={onNavigate} />
      </div>

      {topicPages.length > 0 && (
        <div className="mt-8">
          <h2 className="text-lg font-semibold text-foreground mb-4">{wikiT(lang, "topics")}</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {topicPages.map((topic) => (
              <TopicCard key={topic.id} topic={topic} onClick={() => onNavigate(topic.id)} lang={lang} />
            ))}
          </div>
        </div>
      )}

      <CitationPanel citations={page.citations} />
    </div>
  );
}
