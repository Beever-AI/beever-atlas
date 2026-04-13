import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { WikiMarkdown } from "./WikiMarkdown";
import { CitationPanel } from "./CitationPanel";
import type { WikiPage, WikiCitation } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";

interface QAPair {
  question: string;
  answer: string;
}

interface FaqSection {
  title: string;
  pairs: QAPair[];
}

interface ParsedFaq {
  preamble: string;
  sections: FaqSection[];
  trailer: string; // "Related pages" section and anything after the last ---
}

/**
 * Parse FAQ markdown into structured sections and Q&A pairs.
 * Expects the format produced by FAQ_PROMPT:
 *   ## Section Title
 *   **Q: question text**
 *   A: answer text [N]
 *   ---
 */
function parseFaqMarkdown(raw: string): ParsedFaq {
  // Strip leading h1 (title rendered separately)
  const content = raw.replace(/^#\s+[^\n]+\n*/, "");

  // Split on ## headings — first chunk is the preamble (chart + intro)
  const chunks = content.split(/^(?=##\s)/m);
  const preamble = chunks[0].replace(/^---\s*$/gm, "").trim();

  const sections: FaqSection[] = [];
  let trailer = "";

  for (let i = 1; i < chunks.length; i++) {
    const chunk = chunks[i];
    const newlineIdx = chunk.indexOf("\n");
    const title = (newlineIdx >= 0 ? chunk.slice(2, newlineIdx) : chunk.slice(2)).trim();
    const body = newlineIdx >= 0 ? chunk.slice(newlineIdx + 1) : "";

    // "Related pages" and similar closing sections go into the trailer
    if (/related\s*pages?|see\s*also/i.test(title)) {
      trailer = `## ${title}\n${body}`;
      continue;
    }

    const pairs = parseQAPairs(body);
    if (pairs.length > 0) {
      sections.push({ title, pairs });
    }
  }

  return { preamble, sections, trailer };
}

function parseQAPairs(body: string): QAPair[] {
  const pairs: QAPair[] = [];

  // Split on **Q: ... ** patterns (the question bold marker)
  // Handles: **Q: text** or **Q: text**\n
  const blocks = body.split(/(?=\*\*Q:\s)/);

  for (const block of blocks) {
    const qMatch = block.match(/^\*\*Q:\s*([\s\S]*?)\*\*/);
    if (!qMatch) continue;

    const question = qMatch[1].replace(/\n/g, " ").trim();
    const rest = block.slice(qMatch[0].length).trim();

    // Strip leading "A:" or "**A:**" label
    const answer = rest
      .replace(/^\*\*A:\*\*\s*/m, "")
      .replace(/^A:\s*/m, "")
      .replace(/^---\s*$/gm, "") // strip inline hr separators
      .trim();

    if (question && answer) {
      pairs.push({ question, answer });
    }
  }

  return pairs;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function QACard({ qa, citations }: { qa: QAPair; citations: WikiCitation[] }) {
  const [open, setOpen] = useState(true);

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden transition-all">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-start gap-3 px-4 py-3 text-left hover:bg-muted/30 transition-colors group"
      >
        <span className="shrink-0 mt-0.5 text-primary transition-transform">
          {open
            ? <ChevronDown className="h-4 w-4" />
            : <ChevronRight className="h-4 w-4" />}
        </span>
        <span className="font-semibold text-foreground text-sm leading-snug">{qa.question}</span>
      </button>
      {open && (
        <div className="px-4 pb-4 pt-2 border-t border-border/40 bg-muted/10">
          <WikiMarkdown content={qa.answer} citations={citations} />
        </div>
      )}
    </div>
  );
}

function FaqSection({ section, citations }: { section: FaqSection; citations: WikiCitation[] }) {
  return (
    <section>
      <h2 className="text-lg font-semibold text-foreground mt-6 mb-3 scroll-mt-6">
        {section.title}
      </h2>
      <div className="flex flex-col gap-2">
        {section.pairs.map((qa, i) => (
          <QACard key={i} qa={qa} citations={citations} />
        ))}
      </div>
    </section>
  );
}

// ── Main export ───────────────────────────────────────────────────────────────

interface FaqPageProps {
  page: WikiPage;
  onNavigate: (pageId: string) => void;
  lang?: string;
}

export function FaqPage({ page, onNavigate, lang }: FaqPageProps) {
  const { preamble, sections, trailer } = parseFaqMarkdown(page.content);

  return (
    <div>
      <h1 className="text-2xl font-bold text-foreground">{page.title}</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        {wikiT(lang, "memoriesSuffix", { n: page.memory_count })}
      </p>

      {/* Preamble: chart + intro sentence */}
      {preamble && (
        <div className="mt-6">
          <WikiMarkdown content={preamble} citations={page.citations} onNavigate={onNavigate} />
        </div>
      )}

      {/* Q&A sections */}
      {sections.length > 0 ? (
        <div className="mt-2 divide-y divide-border/0">
          {sections.map((section, i) => (
            <div key={i}>
              <FaqSection section={section} citations={page.citations} />
              {i < sections.length - 1 && <hr className="border-border mt-6" />}
            </div>
          ))}
        </div>
      ) : (
        /* Fallback: render as plain markdown if parsing finds no structured Q&As */
        <div className="mt-6">
          <WikiMarkdown content={page.content.replace(/^#\s+[^\n]+\n*/, "")} citations={page.citations} onNavigate={onNavigate} />
        </div>
      )}

      {/* Trailer: "Related pages" etc. */}
      {trailer && (
        <div className="mt-6">
          <hr className="border-border mb-6" />
          <WikiMarkdown content={trailer} citations={page.citations} onNavigate={onNavigate} />
        </div>
      )}

      <CitationPanel citations={page.citations} />
    </div>
  );
}
