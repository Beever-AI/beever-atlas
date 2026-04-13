import { WikiMarkdown } from "./WikiMarkdown";
import { CitationPanel } from "./CitationPanel";
import type { WikiPage } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";

interface GenericPageProps {
  page: WikiPage;
  onNavigate: (pageId: string) => void;
  lang?: string;
}

export function GenericPage({ page, onNavigate, lang }: GenericPageProps) {
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

      <CitationPanel citations={page.citations} />
    </div>
  );
}
