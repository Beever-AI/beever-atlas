/** Narrative Article module — renders a multi-section article body.
 *
 *  Shape (set by `wiki/modules/narrative_article.py::build_narrative_article_data`):
 *    - sections: array of { anchor, heading, paragraphs[], citations[],
 *                          visual?: { kind, content }, citation_coverage }
 *    - paragraphs[i]: { text, citations[], is_inference }
 *
 *  Layout: article reading column (max-w-prose), section anchor headers,
 *  paragraph styling (leading-relaxed, gap between paragraphs), inline
 *  citation chips, optional supporting visual per section.
 *
 *  This is the new spotlight when present — `ModuleRenderer` renders it
 *  FIRST and demotes the existing 26 modules to a "Reference & Evidence"
 *  appendix below. When `sections` is empty, the component renders
 *  nothing so the page falls back to module-only layout.
 */

import { Fragment, type ReactNode } from "react";
import type { WikiCitation } from "@/lib/types";
import type { ModuleProps } from "./ModuleRenderer";
import { MermaidBlock } from "../MermaidBlock";

// ---------------------------------------------------------------------------
// Data shape
// ---------------------------------------------------------------------------

interface NarrativeParagraph {
  text: string;
  citations: string[];
  is_inference: boolean;
}

type VisualKind =
  | "table"
  | "mermaid"
  | "list"
  | "callout"
  | "code"
  | "blockquote";

interface NarrativeVisual {
  kind: VisualKind;
  content: unknown;
}

interface NarrativeSection {
  anchor: string;
  heading: string;
  paragraphs: NarrativeParagraph[];
  citations: string[];
  visual: NarrativeVisual | null;
  citation_coverage: number;
}

interface NarrativeArticleData {
  label?: string;
  renderer_kind?: string;
  sections?: NarrativeSection[];
  total_words?: number;
  distinct_facts_cited?: number;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Anchor format (M-8): kebab-case alphanumeric, must start with a
 *  letter or digit, max 24 chars. Mirrors
 *  ``_VALID_ANCHOR_RE`` in
 *  ``src/beever_atlas/wiki/modules/narrative_validator.py`` so newly
 *  validated articles satisfy this trivially and pre-existing
 *  persisted articles get sanitised at render time too (defense in
 *  depth). Required so ``getElementById(anchor)`` scroll-snap stays
 *  reliable even when the LLM emits HTML-injection-like strings. */
const VALID_ANCHOR_RE = /^[a-z0-9][a-z0-9-]{0,23}$/;

function sanitizeAnchor(raw: string, fallbackIdx: number): string {
  const trimmed = (raw || "").trim().toLowerCase();
  if (VALID_ANCHOR_RE.test(trimmed)) return trimmed;
  const slug = trimmed
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 24);
  if (VALID_ANCHOR_RE.test(slug)) return slug;
  return `section-${fallbackIdx}`;
}

/** Coerce a section payload from the unknown ``module.data`` blob.
 *
 *  ``fallbackIdx`` is the 1-based position of this section in the
 *  article — used as the fallback for anchor sanitisation so a
 *  malformed anchor still gets a stable ``section-N`` identifier
 *  rather than dropping the whole section.
 */
function coerceSection(raw: unknown, fallbackIdx: number): NarrativeSection | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const rawAnchor = typeof r.anchor === "string" ? r.anchor : "";
  const heading = typeof r.heading === "string" ? r.heading : "";
  if (!heading) return null;
  // M-8 defense-in-depth: backend validator already sanitises, but
  // pre-existing persisted articles or non-validator code paths may
  // emit a non-conforming anchor. Sanitise here so the rendered DOM
  // ``id`` is always a clean kebab-case slug.
  const anchor = sanitizeAnchor(rawAnchor || heading, fallbackIdx);

  const paragraphsRaw = Array.isArray(r.paragraphs) ? r.paragraphs : [];
  const paragraphs: NarrativeParagraph[] = [];
  for (const p of paragraphsRaw) {
    if (!p || typeof p !== "object") continue;
    const pr = p as Record<string, unknown>;
    const text = typeof pr.text === "string" ? pr.text : "";
    if (!text) continue;
    const citationsRaw = Array.isArray(pr.citations) ? pr.citations : [];
    const citations = citationsRaw
      .filter((c): c is string => typeof c === "string" && c.length > 0);
    paragraphs.push({
      text,
      citations,
      is_inference: Boolean(pr.is_inference),
    });
  }
  if (paragraphs.length === 0) return null;

  const citationsRaw = Array.isArray(r.citations) ? r.citations : [];
  const citations = citationsRaw
    .filter((c): c is string => typeof c === "string" && c.length > 0);

  let visual: NarrativeVisual | null = null;
  if (r.visual && typeof r.visual === "object") {
    const vr = r.visual as Record<string, unknown>;
    const kindRaw = typeof vr.kind === "string" ? vr.kind.toLowerCase() : "";
    const allowed: VisualKind[] = [
      "table",
      "mermaid",
      "list",
      "callout",
      "code",
      "blockquote",
    ];
    if ((allowed as string[]).includes(kindRaw)) {
      visual = { kind: kindRaw as VisualKind, content: vr.content };
    }
  }

  const coverage =
    typeof r.citation_coverage === "number" ? r.citation_coverage : 0;

  return {
    anchor,
    heading,
    paragraphs,
    citations,
    visual,
    citation_coverage: coverage,
  };
}

/** Build a Map from fact_id → 1-indexed display number based on the
 *  order of first occurrence across all sections. The frontend uses
 *  this map so chips render as `[1]`, `[2]` etc. with stable indices.
 *
 *  Paragraphs that cite a fact_id NOT in this map fall through to
 *  the dev-only ``[?]`` chip branch in ``ParagraphLine`` (M-3) —
 *  defensive guard against future code paths that supply a
 *  pre-built or filtered index.
 */
function buildFactIdIndex(sections: NarrativeSection[]): Map<string, number> {
  const idx = new Map<string, number>();
  for (const section of sections) {
    for (const p of section.paragraphs) {
      for (const cid of p.citations) {
        if (!idx.has(cid)) {
          idx.set(cid, idx.size + 1);
        }
      }
    }
  }
  return idx;
}

/** Total word count across every paragraph (for the reading-time chip). */
function totalWordCount(sections: NarrativeSection[]): number {
  let total = 0;
  for (const section of sections) {
    for (const p of section.paragraphs) {
      total += p.text.trim().split(/\s+/).filter(Boolean).length;
    }
  }
  return total;
}

/** Reading-time estimate (200 wpm, rounded up to nearest minute). */
function readingTimeMinutes(words: number): number {
  if (words <= 0) return 0;
  return Math.max(1, Math.ceil(words / 200));
}

/** Distinct fact_ids cited across the article. */
function distinctFactCount(sections: NarrativeSection[]): number {
  const seen = new Set<string>();
  for (const section of sections) {
    for (const p of section.paragraphs) {
      for (const cid of p.citations) seen.add(cid);
    }
  }
  return seen.size;
}

// ---------------------------------------------------------------------------
// Inline citation chip (with hover preview)
// ---------------------------------------------------------------------------

/** Track fact_ids we've already warned about so dev-mode console
 *  output does not spam on every render. Module-level Set is fine —
 *  the warnings are diagnostic and the lifecycle of "warned about
 *  this id" is the page session. (M-3) */
const warnedFactIds = new Set<string>();

interface CitationChipProps {
  factId: string;
  displayIndex: number;
  citation?: WikiCitation;
}

function CitationChip({ factId, displayIndex, citation }: CitationChipProps) {
  // Build a native ``title`` string as a zero-cost fallback so the
  // cited fact is reachable even when the popover is not shown
  // (mobile, screen readers, etc.).
  const titleParts: string[] = [];
  if (citation?.text_excerpt) titleParts.push(citation.text_excerpt);
  if (citation?.author) titleParts.push(`— @${citation.author}`);
  if (citation?.timestamp) titleParts.push(citation.timestamp);
  const titleText = titleParts.join(" ");

  // Scroll-to-source behaviour mirrors ``CitationLink`` on the Overview
  // path so a click on any narrative chip jumps to the matching entry
  // in the Sources panel and briefly rings it. ``displayIndex`` is the
  // 1-based number rendered on the chip, which matches the Sources
  // panel's ``id={citation-${i + 1}}`` anchors.
  const handleClick = () => {
    const el = document.getElementById(`citation-${displayIndex}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add(
        "ring-2",
        "ring-primary",
        "ring-offset-2",
        "ring-offset-background",
      );
      setTimeout(
        () =>
          el.classList.remove(
            "ring-2",
            "ring-primary",
            "ring-offset-2",
            "ring-offset-background",
          ),
        2000,
      );
      return;
    }
    // CitationPanel's collapsed state hides citations beyond the first
    // few; dispatch the same event the Overview path uses so the panel
    // can expand and then scroll. Listener lives in CitationPanel.
    window.dispatchEvent(
      new CustomEvent("wiki:citation-jump", {
        detail: { index: displayIndex },
      }),
    );
  };

  return (
    <span className="group/cite relative inline-block align-baseline">
      <button
        type="button"
        onClick={handleClick}
        data-fact-id={factId}
        data-testid="narrative-citation-chip"
        title={titleText || undefined}
        className="inline-flex items-center justify-center min-w-[1.2rem] px-1 py-0.5 mx-0.5 text-[10px] font-semibold leading-none rounded-full bg-primary/10 hover:bg-primary/20 text-primary cursor-pointer transition-colors"
      >
        [{displayIndex}]
      </button>
      {citation && (
        <span
          role="tooltip"
          data-testid="narrative-citation-popover"
          className="pointer-events-none invisible group-hover/cite:visible absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-72 max-w-[calc(100vw-2rem)] rounded-lg bg-popover border border-border px-3 py-2.5 text-xs text-popover-foreground shadow-xl z-50"
        >
          <div className="flex items-center gap-2 mb-1.5">
            {citation.author && (
              <span className="inline-flex items-center rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                @{citation.author}
              </span>
            )}
            {citation.timestamp && (
              <span className="text-muted-foreground text-[10px]">
                {citation.timestamp}
              </span>
            )}
          </div>
          <p className="text-popover-foreground/90 leading-relaxed line-clamp-3">
            {citation.text_excerpt}
          </p>
        </span>
      )}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Visual dispatcher
// ---------------------------------------------------------------------------

interface VisualBlockProps {
  visual: NarrativeVisual;
  factIdIndex: Map<string, number>;
  citationLookup: Map<string, WikiCitation>;
}

/** Wrap ``renderParagraphWithInlineCitations`` for use inside visual
 *  content (list items, table cells, callout/blockquote text). Returns
 *  the interleaved ReactNode array so each call site can splat it
 *  directly into JSX. Code blocks intentionally bypass this — code is
 *  preserved verbatim so any ``[f_xxx]`` inside the snippet survives. */
function renderTextWithInlineCitations(
  text: string,
  factIdIndex: Map<string, number>,
  citationLookup: Map<string, WikiCitation>,
): ReactNode[] {
  const { nodes } = renderParagraphWithInlineCitations(
    text,
    factIdIndex,
    citationLookup,
  );
  return nodes;
}

function VisualBlock({ visual, factIdIndex, citationLookup }: VisualBlockProps) {
  switch (visual.kind) {
    case "table": {
      const content = visual.content as
        | { headers?: unknown; rows?: unknown }
        | undefined
        | null;
      const headers = Array.isArray(content?.headers)
        ? (content!.headers as unknown[]).map((h) => String(h))
        : [];
      const rowsRaw = Array.isArray(content?.rows) ? (content!.rows as unknown[]) : [];
      const rows: string[][] = rowsRaw.map((row) =>
        Array.isArray(row)
          ? (row as unknown[]).map((cell) => String(cell))
          : [],
      );
      if (headers.length === 0 && rows.length === 0) return null;
      return (
        <div
          className="my-4 overflow-x-auto"
          data-testid="narrative-visual-table"
        >
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b border-border">
                {headers.map((h, i) => (
                  <th
                    key={i}
                    scope="col"
                    className="text-left font-semibold px-3 py-2 text-foreground"
                  >
                    {renderTextWithInlineCitations(h, factIdIndex, citationLookup).map(
                      (node, ni) => (
                        <Fragment key={ni}>{node}</Fragment>
                      ),
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr key={ri} className="border-b border-border/50">
                  {row.map((cell, ci) => (
                    <td
                      key={ci}
                      className="px-3 py-2 text-muted-foreground align-top"
                    >
                      {renderTextWithInlineCitations(
                        cell,
                        factIdIndex,
                        citationLookup,
                      ).map((node, ni) => (
                        <Fragment key={ni}>{node}</Fragment>
                      ))}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    case "mermaid": {
      const chart = typeof visual.content === "string" ? visual.content : "";
      if (!chart) return null;
      return (
        <div data-testid="narrative-visual-mermaid">
          <MermaidBlock chart={chart} />
        </div>
      );
    }
    case "list": {
      const content = visual.content as
        | { items?: unknown; ordered?: unknown }
        | undefined
        | null;
      const itemsRaw = Array.isArray(content?.items)
        ? (content!.items as unknown[])
        : [];
      const items = itemsRaw.map((item) => String(item)).filter(Boolean);
      if (items.length === 0) return null;
      const ordered = Boolean(content?.ordered);
      const ListTag = ordered ? "ol" : "ul";
      return (
        <ListTag
          data-testid="narrative-visual-list"
          className={`my-4 ${ordered ? "list-decimal" : "list-disc"} pl-6 space-y-1.5 text-sm text-foreground`}
        >
          {items.map((item, i) => (
            <li key={i} className="leading-relaxed">
              {renderTextWithInlineCitations(item, factIdIndex, citationLookup).map(
                (node, ni) => (
                  <Fragment key={ni}>{node}</Fragment>
                ),
              )}
            </li>
          ))}
        </ListTag>
      );
    }
    case "callout": {
      // Accept BOTH shapes the v3 prompt and earlier docs may have
      // emitted: ``{type, content}`` (older docs) and
      // ``{variant, text}`` (the v3 prompt's documented schema, see
      // ``wiki/prompts.py`` callout example). Picking either canonical
      // form would break the other; tolerating both keeps existing
      // persisted articles renderable AND matches the prompt today.
      // ``info`` is treated as ``note`` (the prompt allows
      // ``warning|info|tip`` but the underlying CalloutBox component
      // only knows ``note|tip|warning``).
      const content = visual.content as
        | {
            type?: unknown;
            variant?: unknown;
            content?: unknown;
            text?: unknown;
          }
        | undefined
        | null;
      const variantRaw =
        typeof content?.variant === "string"
          ? content.variant.toLowerCase()
          : typeof content?.type === "string"
            ? content.type.toLowerCase()
            : "note";
      const type: "note" | "tip" | "warning" =
        variantRaw === "tip"
          ? "tip"
          : variantRaw === "warning"
            ? "warning"
            : "note";
      const text =
        typeof content?.text === "string"
          ? content.text
          : typeof content?.content === "string"
            ? content.content
            : "";
      if (!text) return null;
      const calloutNodes = renderTextWithInlineCitations(
        text,
        factIdIndex,
        citationLookup,
      );
      // Inline the CalloutBox styles here so the body can carry the
      // interleaved citation chips. ``CalloutBox`` accepts only a
      // string ``content`` prop today; copying the per-variant style
      // map keeps that contract intact while letting visual content
      // citations render in-place inside the callout body.
      const calloutStyles: Record<
        "note" | "tip" | "warning",
        { container: string; text: string; label: string }
      > = {
        note: {
          container: "bg-blue-500/10 border-blue-500/30",
          text: "text-blue-400",
          label: "Note",
        },
        tip: {
          container: "bg-emerald-500/10 border-emerald-500/30",
          text: "text-emerald-400",
          label: "Tip",
        },
        warning: {
          container: "bg-amber-500/10 border-amber-500/30",
          text: "text-amber-400",
          label: "Warning",
        },
      };
      const cs = calloutStyles[type];
      return (
        <div data-testid="narrative-visual-callout">
          <div className={`my-3 rounded-lg border ${cs.container} p-4`}>
            <p className={`text-sm font-semibold ${cs.text}`}>{cs.label}</p>
            <p className={`mt-1 text-sm ${cs.text} opacity-90`}>
              {calloutNodes.map((node, ni) => (
                <Fragment key={ni}>{node}</Fragment>
              ))}
            </p>
          </div>
        </div>
      );
    }
    case "code": {
      const content = visual.content as
        | { language?: unknown; code?: unknown; content?: unknown }
        | string
        | undefined
        | null;
      const code =
        typeof content === "string"
          ? content
          : typeof content?.code === "string"
            ? content.code
            : typeof content?.content === "string"
              ? content.content
              : "";
      if (!code) return null;
      const language =
        typeof content === "object" && content && typeof content.language === "string"
          ? content.language
          : "";
      return (
        <pre
          data-testid="narrative-visual-code"
          className="my-4 p-4 rounded-lg bg-muted/40 border border-border overflow-x-auto text-xs"
        >
          <code className={language ? `language-${language}` : undefined}>
            {code}
          </code>
        </pre>
      );
    }
    case "blockquote": {
      const content = visual.content as
        | { content?: unknown; text?: unknown; attribution?: unknown }
        | string
        | undefined
        | null;
      const text =
        typeof content === "string"
          ? content
          : typeof content?.content === "string"
            ? content.content
            : typeof content?.text === "string"
              ? content.text
              : "";
      if (!text) return null;
      const attribution =
        typeof content === "object" && content && typeof content.attribution === "string"
          ? content.attribution
          : "";
      return (
        <blockquote
          data-testid="narrative-visual-blockquote"
          className="my-4 border-l-4 border-muted pl-4 italic text-muted-foreground"
        >
          <p className="text-sm leading-relaxed">
            {renderTextWithInlineCitations(text, factIdIndex, citationLookup).map(
              (node, ni) => (
                <Fragment key={ni}>{node}</Fragment>
              ),
            )}
          </p>
          {attribution && (
            <footer className="mt-1 text-xs not-italic text-muted-foreground/70">
              — {attribution}
            </footer>
          )}
        </blockquote>
      );
    }
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// Paragraph renderer (with inline citation chip injection)
// ---------------------------------------------------------------------------

interface ParagraphProps {
  paragraph: NarrativeParagraph;
  factIdIndex: Map<string, number>;
  citationLookup: Map<string, WikiCitation>;
}

/** Match `[f_xxx]` or `[f_xxx, f_yyy, ...]` inline citation patterns
 *  the v3 prompt emits inside paragraph text. The regex only matches
 *  fact-id chains separated by commas — anything outside that shape
 *  passes through as literal text. */
const INLINE_CITATION_RE = /\[(f_[a-zA-Z0-9_]+(?:\s*,\s*f_[a-zA-Z0-9_]+)*)\]/g;

/** Render a single citation chip for ``factId``, falling back to the
 *  dev-only ``[?]`` missing chip when the id is not in the index. */
function renderChipForFactId(
  factId: string,
  factIdIndex: Map<string, number>,
  citationLookup: Map<string, WikiCitation>,
  keyHint: string,
): ReactNode {
  const displayIndex = factIdIndex.get(factId);
  if (displayIndex === undefined) {
    // M-3: silently dropping a missing-fact_id citation hides
    // upgrade-time data drift. In dev render a dim ``[?]`` chip
    // with ``data-fact-id`` so authors notice; in prod fall back
    // to the prior "render nothing" behaviour so users never see
    // diagnostic UI.
    if (import.meta.env.DEV) {
      if (!warnedFactIds.has(factId)) {
        // eslint-disable-next-line no-console -- dev-only diagnostic
        console.warn(
          `[NarrativeArticle] citation references missing fact_id: ${factId}`,
        );
        warnedFactIds.add(factId);
      }
      return (
        <span
          key={keyHint}
          data-testid="narrative-citation-chip-missing"
          data-fact-id={factId}
          title={`Missing fact: ${factId}`}
          className="inline-flex items-center px-1 py-0.5 mx-0.5 text-[10px] font-medium leading-none rounded bg-amber-500/20 text-amber-700 dark:text-amber-400 cursor-help"
        >
          [?]
        </span>
      );
    }
    return null;
  }
  return (
    <CitationChip
      key={keyHint}
      factId={factId}
      displayIndex={displayIndex}
      citation={citationLookup.get(factId)}
    />
  );
}

/** Parse ``[f_xxx]`` / ``[f_xxx, f_yyy]`` markers out of the paragraph
 *  text and replace each with citation chip elements at the original
 *  position. Returns the interleaved nodes plus a flag the caller uses
 *  to know whether ANY inline marker was found — when no markers are
 *  present, the caller falls back to trailing-chip rendering for
 *  backward compat with persisted articles authored before the v3
 *  prompt began emitting inline patterns.
 */
function renderParagraphWithInlineCitations(
  text: string,
  factIdIndex: Map<string, number>,
  citationLookup: Map<string, WikiCitation>,
): { nodes: ReactNode[]; foundInlineCitations: boolean } {
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let matchIdx = 0;
  let foundInlineCitations = false;
  // Reset lastIndex defensively — the regex object is module-scope.
  INLINE_CITATION_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = INLINE_CITATION_RE.exec(text)) !== null) {
    foundInlineCitations = true;
    const start = match.index;
    const end = start + match[0].length;
    if (start > cursor) {
      nodes.push(text.slice(cursor, start));
    }
    const factIds = match[1]
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    factIds.forEach((factId, i) => {
      if (i > 0) nodes.push(", ");
      nodes.push(
        renderChipForFactId(
          factId,
          factIdIndex,
          citationLookup,
          `inline-${matchIdx}-${i}-${factId}`,
        ),
      );
    });
    matchIdx += 1;
    cursor = end;
  }
  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }
  return { nodes, foundInlineCitations };
}

/** Render the paragraph text. The v3 prompt embeds ``[f_xxx]`` markers
 *  inline within ``paragraph.text`` so citations sit at the position
 *  the writer chose. We parse those markers and replace each with a
 *  ``CitationChip`` at the same position. When NO inline markers are
 *  present (older persisted articles authored before the inline-marker
 *  prompt change) we fall back to trailing chips at the end of the
 *  paragraph so existing pages keep rendering correctly.
 */
function ParagraphLine({ paragraph, factIdIndex, citationLookup }: ParagraphProps) {
  const { nodes: inlineNodes, foundInlineCitations } =
    renderParagraphWithInlineCitations(
      paragraph.text,
      factIdIndex,
      citationLookup,
    );

  // Backward-compat fallback: pre-inline-marker pages have no
  // ``[f_xxx]`` patterns in the text but still carry a populated
  // ``paragraph.citations`` list — render those as trailing chips.
  const trailingChips: ReactNode[] = [];
  if (!foundInlineCitations) {
    paragraph.citations.forEach((cid, i) => {
      const chip = renderChipForFactId(
        cid,
        factIdIndex,
        citationLookup,
        `trailing-${i}-${cid}`,
      );
      if (chip !== null) trailingChips.push(chip);
    });
  }

  return (
    <p
      data-testid="narrative-paragraph"
      data-is-inference={paragraph.is_inference ? "true" : "false"}
      className="text-[15px] leading-relaxed text-foreground/90"
    >
      {paragraph.is_inference && (
        <span
          data-testid="narrative-inference-chip"
          className="inline-flex items-center px-1.5 py-0.5 mr-1.5 text-[10px] font-medium rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 align-middle"
        >
          [agent-inference]
        </span>
      )}
      {inlineNodes.map((node, i) => (
        <Fragment key={i}>{node}</Fragment>
      ))}
      {trailingChips.length > 0 && (
        <span className="ml-0.5">
          {trailingChips.map((chip, i) => (
            <Fragment key={i}>{chip}</Fragment>
          ))}
        </span>
      )}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function NarrativeArticleModule({ module, citations }: ModuleProps) {
  const data = (module.data ?? {}) as NarrativeArticleData;
  const sectionsRaw = Array.isArray(data.sections) ? data.sections : [];
  const sections: NarrativeSection[] = [];
  for (let i = 0; i < sectionsRaw.length; i += 1) {
    const cleaned = coerceSection(sectionsRaw[i], i + 1);
    if (cleaned) sections.push(cleaned);
  }
  if (sections.length === 0) {
    return null;
  }

  // Build the fact_id → 1-indexed display number map and the
  // fact_id → WikiCitation lookup.
  //
  // The backend WikiCitation.id is "[1]", "[2]", … (a display marker),
  // while paragraph citations carry the underlying AtomicFact.id
  // (``f_xxx``). The lookup is keyed on ``fact_id`` so chip popovers
  // resolve. We tolerate older payloads that lack ``fact_id`` by also
  // indexing on ``id`` (no-op when ``id`` is the bracketed display
  // marker — which never matches a real fact_id query).
  const factIdIndex = buildFactIdIndex(sections);
  const citationLookup = new Map<string, WikiCitation>();
  for (const c of citations) {
    if (c?.fact_id) citationLookup.set(c.fact_id, c);
    else if (c?.id) citationLookup.set(c.id, c);
  }

  const words = totalWordCount(sections);
  const minutes = readingTimeMinutes(words);
  const distinctFacts = distinctFactCount(sections);

  return (
    <article
      data-testid="narrative-article"
      id={`module-${module.anchor}`}
      className="mx-auto max-w-prose mt-2 mb-10"
      data-toc-skip
    >
      <header className="mb-6 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        {minutes > 0 && (
          <span
            data-testid="narrative-reading-time"
            className="inline-flex items-center gap-1 rounded-md bg-muted/50 px-2 py-1"
          >
            <span aria-hidden="true">⏱</span>
            <span>
              {minutes} min read
            </span>
          </span>
        )}
        {distinctFacts > 0 && (
          <span
            data-testid="narrative-memories-synthesized"
            className="inline-flex items-center gap-1 rounded-md bg-muted/50 px-2 py-1"
          >
            <span aria-hidden="true">🧠</span>
            <span>
              {distinctFacts} {distinctFacts === 1 ? "memory" : "memories"} synthesized
            </span>
          </span>
        )}
      </header>

      {sections.map((section) => (
        <section
          key={section.anchor}
          data-testid="narrative-section"
          className="mb-8 scroll-mt-20"
        >
          <h2
            id={section.anchor}
            data-testid="narrative-section-heading"
            className="text-xl font-semibold text-foreground mb-3 scroll-mt-20"
          >
            {section.heading}
          </h2>
          <div className="space-y-3">
            {section.paragraphs.map((paragraph, i) => (
              <ParagraphLine
                key={i}
                paragraph={paragraph}
                factIdIndex={factIdIndex}
                citationLookup={citationLookup}
              />
            ))}
          </div>
          {section.visual && (
            <VisualBlock
              visual={section.visual}
              factIdIndex={factIdIndex}
              citationLookup={citationLookup}
            />
          )}
        </section>
      ))}
    </article>
  );
}
