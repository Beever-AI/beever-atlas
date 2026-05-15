import { useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  ChevronsDownUp,
  ChevronsUpDown,
  HelpCircle,
  Search,
  X,
} from "lucide-react";
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
 *
 * Tolerates ``null``/``undefined`` content so a page from the modular
 * pipeline (where the body lives in ``modules[]`` instead of a
 * top-level ``content`` string) does not crash the renderer.
 */
/** Walks the FAQ markdown line-by-line and extracts Q&A pairs grouped
 *  by the most recent heading. Tolerant of nested heading depths and
 *  mixed shapes (``**Q: ...** + A:`` form, ``### Question?`` form,
 *  ``**Question?**`` bold form). The caller renders each section as
 *  a chevron-collapsible card list — duplicate sections (same title)
 *  are merged so a wrapper-h2 followed by inner topic headings
 *  doesn't render the same Q&A cards twice.
 */
function parseFaqMarkdown(raw: string | null | undefined): ParsedFaq {
  if (!raw) {
    return { preamble: "", sections: [], trailer: "" };
  }
  // Strip leading h1 (title rendered separately)
  const content = raw.replace(/^#\s+[^\n]+\n*/, "");

  const lines = content.split("\n");

  // Phrases that indicate "this heading is a wrapper, not a topic" —
  // skip it as a section title so the rendered cards don't duplicate
  // every topic under both the wrapper AND its nested headings.
  const WRAPPER_RE = /^(frequently\s+asked\s+questions?|faqs?|q\s*&?\s*a)$/i;

  const sections: FaqSection[] = [];
  const preambleLines: string[] = [];
  const trailerLines: string[] = [];
  let trailerStarted = false;

  // Current state during the walk.
  let currentTitle: string | null = null;
  let currentPairs: QAPair[] = [];
  let currentBuffer: string[] = []; // body lines for the current heading
  let pendingQuestion: string | null = null;
  let pendingAnswerLines: string[] = [];

  const flushPendingQA = () => {
    if (pendingQuestion && pendingAnswerLines.length > 0) {
      const answer = pendingAnswerLines
        .join("\n")
        .replace(/^---\s*$/gm, "")
        .trim();
      if (answer) {
        currentPairs.push({ question: pendingQuestion, answer });
      }
    }
    pendingQuestion = null;
    pendingAnswerLines = [];
  };

  const flushSection = () => {
    flushPendingQA();
    // If the current heading wasn't recognised as having pending
    // bold-form Q&As, try the canonical ``**Q: text** / A:`` form
    // on the buffered body as a fallback.
    if (currentPairs.length === 0 && currentBuffer.length > 0) {
      const fromBuffer = parseQAPairs(currentBuffer.join("\n"));
      if (fromBuffer.length > 0) currentPairs = fromBuffer;
    }
    if (currentTitle !== null && currentPairs.length > 0) {
      // Merge with an existing same-title section (handles the
      // wrapper-h2 → topic-h2 duplication case).
      const existing = sections.find((s) => s.title === currentTitle);
      if (existing) {
        existing.pairs.push(...currentPairs);
      } else {
        sections.push({ title: currentTitle, pairs: currentPairs });
      }
    }
    currentTitle = null;
    currentPairs = [];
    currentBuffer = [];
  };

  for (let idx = 0; idx < lines.length; idx += 1) {
    const line = lines[idx];

    // Heading detection — h2 / h3 / h4. The role depends on the
    // heading text: a heading ending with ``?`` is a QUESTION (the
    // current FAQ_PROMPT drift emits questions as h3 headings);
    // otherwise it's a section / topic title.
    const headingMatch = line.match(/^(#{2,4})\s+(.+?)\s*$/);
    if (headingMatch) {
      const title = headingMatch[2].trim();

      // Trailer: ``## Related pages`` / ``See also`` flips us into
      // trailer mode. Everything after goes into the raw trailer
      // string (renderers handle that as plain markdown).
      if (/related\s*pages?|see\s*also/i.test(title)) {
        flushSection();
        trailerStarted = true;
        trailerLines.push(line);
        continue;
      }
      if (trailerStarted) {
        trailerLines.push(line);
        continue;
      }

      // Wrapper phrase — drop the heading itself, keep collecting
      // children under the next real heading.
      if (WRAPPER_RE.test(title)) {
        flushSection();
        continue;
      }

      // Heading-as-question: the current FAQ_PROMPT shape emits
      // each question as ``### Question?`` (h3 ending with ``?``).
      // Treat these as questions, not topic dividers, so card
      // rendering works without a separate ``**Q?**`` bold marker.
      if (/\?\s*$/.test(title) && currentTitle !== null) {
        flushPendingQA();
        pendingQuestion = title;
        continue;
      }

      // Real topic heading — start a new section.
      flushSection();
      currentTitle = title;
      continue;
    }

    if (trailerStarted) {
      trailerLines.push(line);
      continue;
    }

    // Bold-form question (``**...?**``) — opens a new pending QA.
    const boldQMatch = line.match(/^\*\*([^*]+\?)\*\*\s*$/);
    if (boldQMatch && currentTitle !== null) {
      flushPendingQA();
      pendingQuestion = boldQMatch[1].trim();
      continue;
    }

    // Body line — either part of the answer to a pending bold-Q or
    // (when there's no pending Q) buffered for the
    // ``**Q: text** / A:`` fallback path.
    if (pendingQuestion !== null) {
      pendingAnswerLines.push(line);
    } else if (currentTitle !== null) {
      currentBuffer.push(line);
    } else {
      preambleLines.push(line);
    }
  }
  flushSection();

  const preamble = preambleLines.join("\n").replace(/^---\s*$/gm, "").trim();
  const trailer = trailerLines.join("\n").trim();

  return { preamble, sections, trailer };
}

function parseQAPairs(body: string): QAPair[] {
  const pairs: QAPair[] = [];

  // Path A — canonical ``**Q: text** / A: answer`` form (FAQ_PROMPT
  // contract). Detected first so the legacy persistence keeps working.
  if (/\*\*Q:\s/.test(body)) {
    const blocks = body.split(/(?=\*\*Q:\s)/);
    for (const block of blocks) {
      const qMatch = block.match(/^\*\*Q:\s*([\s\S]*?)\*\*/);
      if (!qMatch) continue;
      const question = qMatch[1].replace(/\n/g, " ").trim();
      const rest = block.slice(qMatch[0].length).trim();
      const answer = rest
        .replace(/^\*\*A:\*\*\s*/m, "")
        .replace(/^A:\s*/m, "")
        .replace(/^---\s*$/gm, "")
        .trim();
      if (question && answer) {
        pairs.push({ question, answer });
      }
    }
    return pairs;
  }

  // Path B — ``### Question?\n\nAnswer paragraph`` form. Modern
  // prompts have drifted to emitting questions as h3 headings with
  // free-form prose underneath; without this path the FaqPage falls
  // through to plain markdown rendering and loses the card layout.
  // Each ``###`` h3 starts a new Q&A; everything until the next
  // ``###`` (or end of body) is the answer.
  if (/^###\s/m.test(body)) {
    const blocks = body.split(/(?=^###\s)/m);
    for (const block of blocks) {
      const qMatch = block.match(/^###\s+([^\n]+)\n([\s\S]*)/);
      if (!qMatch) continue;
      const question = qMatch[1].trim();
      const answer = qMatch[2]
        .replace(/^---\s*$/gm, "")
        .trim();
      if (question && answer) {
        pairs.push({ question, answer });
      }
    }
    return pairs;
  }

  // Path C — ``**Question?**\nAnswer paragraph`` form. Bold-prefixed
  // questions without the ``Q:`` marker. Each bold line that ends
  // with ``?`` starts a new pair; the prose until the next bold line
  // is the answer.
  if (/^\*\*[^*]+\?\*\*/m.test(body)) {
    const blocks = body.split(/(?=^\*\*[^*]+\?\*\*)/m);
    for (const block of blocks) {
      const qMatch = block.match(/^\*\*([^*]+\?)\*\*\s*\n?([\s\S]*)/);
      if (!qMatch) continue;
      const question = qMatch[1].trim();
      const answer = qMatch[2]
        .replace(/^---\s*$/gm, "")
        .trim();
      if (question && answer) {
        pairs.push({ question, answer });
      }
    }
    return pairs;
  }

  return pairs;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function QACard({
  qa,
  citations,
  forceOpen,
}: {
  qa: QAPair;
  citations: WikiCitation[];
  /** When the parent toggles "Expand all" / "Collapse all" we
   *  pass a forced state through this prop. ``undefined`` means
   *  the card uses its own local toggle. */
  forceOpen?: boolean;
}) {
  const [localOpen, setLocalOpen] = useState(true);
  const open = forceOpen !== undefined ? forceOpen : localOpen;

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden transition-all">
      <button
        onClick={() => setLocalOpen(!open)}
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

function FaqSection({
  section,
  citations,
  forceOpen,
}: {
  section: FaqSection;
  citations: WikiCitation[];
  forceOpen?: boolean;
}) {
  const slug = section.title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return (
    <section className="mt-8">
      <div
        id={slug}
        className="flex items-baseline gap-2 mb-3 scroll-mt-6 border-b border-border/60 pb-1.5"
      >
        <h2 className="text-base font-semibold text-foreground">
          {section.title}
        </h2>
        <span className="inline-flex items-center rounded-full bg-muted/60 text-muted-foreground text-[10px] font-medium px-1.5 py-0.5 tabular-nums">
          {section.pairs.length}
          {" "}
          {section.pairs.length === 1 ? "question" : "questions"}
        </span>
      </div>
      <div className="flex flex-col gap-2">
        {section.pairs.map((qa, i) => (
          <QACard key={i} qa={qa} citations={citations} forceOpen={forceOpen} />
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
  const [query, setQuery] = useState("");
  const [forceOpen, setForceOpen] = useState<boolean | undefined>(undefined);

  // Filter sections + pairs by query (matches both question and answer
  // text, case-insensitive). Empty query = all sections rendered.
  const filteredSections = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sections;
    const out: FaqSection[] = [];
    for (const section of sections) {
      const matchedPairs = section.pairs.filter(
        (qa) =>
          qa.question.toLowerCase().includes(q) ||
          qa.answer.toLowerCase().includes(q),
      );
      const sectionTitleMatches = section.title.toLowerCase().includes(q);
      if (sectionTitleMatches) {
        // If the section title itself matches, keep ALL its pairs
        // (the user is filtering by topic).
        out.push({ title: section.title, pairs: section.pairs });
      } else if (matchedPairs.length > 0) {
        out.push({ title: section.title, pairs: matchedPairs });
      }
    }
    return out;
  }, [sections, query]);

  const totalQuestions = sections.reduce((n, s) => n + s.pairs.length, 0);
  const filteredQuestions = filteredSections.reduce(
    (n, s) => n + s.pairs.length,
    0,
  );

  return (
    <div>
      <h1 className="text-2xl font-bold text-foreground">{page.title}</h1>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
        <span>{wikiT(lang, "memoriesSuffix", { n: page.memory_count })}</span>
        {totalQuestions > 0 && (
          <span className="inline-flex items-center gap-1">
            <HelpCircle className="h-3.5 w-3.5" aria-hidden="true" />
            {totalQuestions} {totalQuestions === 1 ? "question" : "questions"}
            {sections.length > 1 && ` across ${sections.length} topics`}
          </span>
        )}
      </div>

      {/* Preamble: chart + intro sentence */}
      {preamble && (
        <div className="mt-6">
          <WikiMarkdown content={preamble} citations={page.citations} onNavigate={onNavigate} />
        </div>
      )}

      {/* Toolbar — search + expand/collapse all. Only renders when there
          are real Q&A sections; for empty/fallback FAQs the toolbar
          would have nothing meaningful to do. */}
      {sections.length > 0 && (
        <div
          data-testid="faq-toolbar"
          className="mt-6 mb-2 flex flex-wrap items-center gap-2"
        >
          <div className="relative flex-1 min-w-[200px]">
            <Search
              className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground/60 pointer-events-none"
              aria-hidden="true"
            />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search questions or answers…"
              className="w-full bg-card border border-border rounded-md pl-8 pr-8 py-1.5 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-primary/40 focus:border-primary/60"
              data-testid="faq-search-input"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery("")}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 p-1 rounded text-muted-foreground/70 hover:text-foreground hover:bg-muted/40"
                aria-label="Clear search"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
          <button
            type="button"
            onClick={() => setForceOpen(forceOpen === true ? undefined : true)}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card hover:bg-muted/40 px-2.5 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
            data-testid="faq-expand-all"
          >
            <ChevronsUpDown className="h-3.5 w-3.5" />
            Expand all
          </button>
          <button
            type="button"
            onClick={() => setForceOpen(forceOpen === false ? undefined : false)}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card hover:bg-muted/40 px-2.5 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
            data-testid="faq-collapse-all"
          >
            <ChevronsDownUp className="h-3.5 w-3.5" />
            Collapse all
          </button>
        </div>
      )}

      {/* Search-result summary line */}
      {sections.length > 0 && query && (
        <p
          className="text-xs text-muted-foreground mb-3"
          data-testid="faq-search-summary"
        >
          {filteredQuestions > 0
            ? `Showing ${filteredQuestions} of ${totalQuestions} questions matching "${query}"`
            : `No questions match "${query}"`}
        </p>
      )}

      {/* Q&A sections */}
      {filteredSections.length > 0 ? (
        <div className="mt-2 divide-y divide-border/0">
          {filteredSections.map((section, i) => (
            <FaqSection
              key={i}
              section={section}
              citations={page.citations}
              forceOpen={forceOpen}
            />
          ))}
        </div>
      ) : sections.length > 0 ? (
        /* Sections exist but all filtered out by query — show empty
         * search state instead of falling through to markdown. */
        <div className="mt-6 rounded-lg border border-dashed border-border bg-muted/10 px-6 py-10 text-center text-sm text-muted-foreground">
          No questions match your search.
        </div>
      ) : page.content ? (
        /* Fallback: render as plain markdown if parsing finds no structured Q&As */
        <div className="mt-6">
          <WikiMarkdown content={page.content.replace(/^#\s+[^\n]+\n*/, "")} citations={page.citations} onNavigate={onNavigate} />
        </div>
      ) : (
        /* No structured Q&A AND no plain markdown body — happens when the
         * FAQ page exists in the page_store but compilation produced no
         * content (e.g., channel was too small to populate questions).
         * Render an honest empty state instead of a black screen. */
        <div className="mt-6 rounded-lg border border-dashed border-border bg-muted/10 px-6 py-10 text-center text-sm text-muted-foreground">
          No FAQ available yet — synced messages haven't produced enough Q&amp;A signal.
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
