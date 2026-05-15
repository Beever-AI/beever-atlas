/** Key Facts module v2 — frontend card list.
 *
 *  Renders a severity-grouped, collapsible card list of facts. Critical
 *  facts (importance == "critical") promote to a flat strip at the top
 *  outside any group. Remaining facts group by `fact_type`
 *  (Decisions, Observations, Open Questions, Action Items, Opinions),
 *  each rendering up to 3 by default with a "Show N more" expander.
 *
 *  Each row: severity dot + first-sentence title + author chip + date
 *  subline. Click expands to full body + citation + source link.
 *
 *  When ≥80% of rows share one author, the per-row author chip is
 *  hidden in favor of a single `by Author Name (× N)` header on the
 *  group/section.
 *
 *  History: v1 used `MarkdownModule` to render a backend-emitted GFM
 *  table. v2 reads `module.data.items` directly. See
 *  `wiki/modules/key_facts.py::build_key_facts_data` for the payload
 *  shape.
 */

import { useMemo, useState } from "react";
import type { ModuleProps } from "./ModuleRenderer";
import { SeverityBadge } from "../SeverityBadge";
import { applyGlossaryToNodes, type GlossaryMap } from "@/lib/glossaryHighlight";

// ---------------------------------------------------------------------------
// Types — mirror the Python builder's output shape exactly.
// ---------------------------------------------------------------------------

type Severity = "critical" | "high" | "medium" | "low";

interface KeyFactItem {
  fact_id: string;
  title: string;
  body: string;
  fact_type: string;
  importance: Severity | string;
  author: { name: string; id: string };
  ts: string;
  source: { url: string; platform: string };
  citations: unknown[];
}

interface KeyFactsData {
  label?: string;
  renderer_kind?: string;
  items?: KeyFactItem[];
  groups?: string[];
  /** Optional channel glossary, term → definition. When provided,
   *  matching whole words inside fact title/body get a dotted
   *  underline + native ``title`` tooltip via ``applyGlossaryToNodes``. */
  glossary?: GlossaryMap;
}

// ---------------------------------------------------------------------------
// Visual helpers
// ---------------------------------------------------------------------------

const SEVERITY_BORDER_CLASS: Record<Severity, string> = {
  critical: "border-l-red-500",
  high: "border-l-amber-500",
  medium: "border-l-blue-500",
  low: "border-l-muted-foreground/40",
};

const TYPE_LABEL_OVERRIDES: Record<string, string> = {
  decision: "Decisions",
  observation: "Observations",
  open_question: "Open Questions",
  action_item: "Action Items",
  opinion: "Opinions",
  claim: "Observations",
  event: "Observations",
};

function humanizeFactType(raw: string): string {
  if (!raw) return "Observations";
  const key = raw.toLowerCase();
  if (TYPE_LABEL_OVERRIDES[key]) return TYPE_LABEL_OVERRIDES[key];
  // Fallback: snake_case → Title Case + plural ("s") if not already.
  const words = key.split(/[_\s]+/).filter(Boolean);
  const title = words
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
  if (title.endsWith("s")) return title;
  return title + "s";
}

function severityFor(item: KeyFactItem): Severity {
  const v = String(item.importance || "").toLowerCase();
  if (v === "critical" || v === "high" || v === "medium" || v === "low") {
    return v;
  }
  return "medium";
}

const URL_REGEX = /(https?:\/\/[^\s)<>]+[^\s.,;:!?)<>])/gi;

/** Convert URLs in a text fragment into clickable anchor tags. Returns
 *  a React node array that can be rendered directly. Pure function —
 *  no DOM manipulation, safe to call inside render. */
function linkifyText(text: string): (string | React.ReactNode)[] {
  if (!text) return [text];
  const parts: (string | React.ReactNode)[] = [];
  let lastIndex = 0;
  // Reset the regex's lastIndex to avoid state leakage between calls.
  URL_REGEX.lastIndex = 0;
  let match: RegExpExecArray | null;
  let counter = 0;
  while ((match = URL_REGEX.exec(text)) !== null) {
    const start = match.index;
    if (start > lastIndex) {
      parts.push(text.slice(lastIndex, start));
    }
    parts.push(
      <a
        key={`url-${counter}-${start}`}
        href={match[0]}
        target="_blank"
        rel="noopener noreferrer"
        className="underline decoration-dotted text-primary hover:text-primary/80"
      >
        {match[0]}
      </a>,
    );
    lastIndex = start + match[0].length;
    counter += 1;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return parts;
}

function relativeDate(ts: string): string {
  if (!ts) return "";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  const now = Date.now();
  const diff = now - d.getTime();
  const dayMs = 1000 * 60 * 60 * 24;
  const days = Math.floor(diff / dayMs);
  if (days < 0) return d.toLocaleDateString();
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface FactRowProps {
  item: KeyFactItem;
  /** When true, the per-row author chip is hidden because the parent
   *  group already shows a single "by X (× N)" header. */
  hideAuthor: boolean;
  /** Optional glossary applied to title/body text — wraps matched
   *  acronyms in dotted-underline ``<span title=...>`` for inline
   *  hover-defs. */
  glossary?: GlossaryMap;
}

function FactRow({ item, hideAuthor, glossary }: FactRowProps) {
  const [expanded, setExpanded] = useState(false);
  const severity = severityFor(item);
  const titleLinkified = useMemo(
    () => applyGlossaryToNodes(linkifyText(item.title), glossary),
    [item.title, glossary],
  );
  const bodyLinkified = useMemo(
    () => applyGlossaryToNodes(linkifyText(item.body), glossary),
    [item.body, glossary],
  );
  const isCritical = severity === "critical";

  return (
    <div
      data-testid={`key-fact-row-${item.fact_id || item.title.slice(0, 20)}`}
      className={
        "border-l-[3px] " +
        SEVERITY_BORDER_CLASS[severity] +
        " bg-card/50 rounded-r-md px-3 py-2 hover:bg-card transition-colors"
      }
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full text-left flex items-start gap-2"
        aria-expanded={expanded}
      >
        <SeverityBadge
          severity={severity}
          iconSize={12}
          className="mt-1"
        />
        <div className="flex-1 min-w-0">
          <div
            className={
              "text-sm leading-snug " +
              (isCritical ? "font-medium" : "")
            }
          >
            {titleLinkified}
          </div>
          {(!hideAuthor && item.author.name) || item.ts ? (
            <div className="text-xs text-muted-foreground mt-0.5 flex items-center gap-2 flex-wrap">
              {!hideAuthor && item.author.name && (
                <span className="inline-flex items-center gap-1">
                  <span className="h-4 w-4 rounded-full bg-muted text-[10px] flex items-center justify-center">
                    {item.author.name.charAt(0).toUpperCase()}
                  </span>
                  {item.author.name}
                </span>
              )}
              {item.ts && <span>{relativeDate(item.ts)}</span>}
            </div>
          ) : null}
        </div>
      </button>
      {expanded && (
        <div className="mt-2 pl-4 text-sm text-muted-foreground">
          <div className="whitespace-pre-wrap leading-relaxed">
            {bodyLinkified}
          </div>
          <div className="mt-2 flex items-center gap-3 text-xs">
            {Array.isArray(item.citations) && item.citations.length > 0 && (
              <span className="px-2 py-0.5 rounded bg-muted text-muted-foreground/80">
                {item.citations.length} citation
                {item.citations.length === 1 ? "" : "s"}
              </span>
            )}
            {item.source.url && (
              <a
                href={item.source.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-primary hover:underline"
              >
                source ↗
              </a>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

interface GroupSectionProps {
  factType: string;
  items: KeyFactItem[];
  glossary?: GlossaryMap;
}

function GroupSection({ factType, items, glossary }: GroupSectionProps) {
  const [showAll, setShowAll] = useState(false);
  const total = items.length;
  const visibleCount = showAll ? total : Math.min(3, total);
  const visibleItems = items.slice(0, visibleCount);
  const remaining = total - visibleCount;

  // Author dedup: when ≥80% of rows share one author, hide the per-row
  // author chip and surface a single `by X (× N)` header.
  const { dominantAuthor, dominantCount } = useMemo(() => {
    const counts = new Map<string, number>();
    for (const it of items) {
      const name = it.author.name;
      if (!name) continue;
      counts.set(name, (counts.get(name) || 0) + 1);
    }
    let topName: string | null = null;
    let topCount = 0;
    for (const [name, c] of counts) {
      if (c > topCount) {
        topName = name;
        topCount = c;
      }
    }
    return { dominantAuthor: topName, dominantCount: topCount };
  }, [items]);

  const dedupAuthor =
    dominantAuthor && total > 0 && dominantCount / total >= 0.8;

  return (
    <section
      className="mt-4"
      data-testid={`key-facts-group-${factType}`}
    >
      <header className="flex items-baseline justify-between mb-2">
        <h3 className="text-sm font-semibold text-foreground">
          {humanizeFactType(factType)}
          <span className="ml-2 text-xs font-normal text-muted-foreground">
            {total}
          </span>
        </h3>
        {dedupAuthor && (
          <span className="text-xs text-muted-foreground">
            by {dominantAuthor} (× {dominantCount})
          </span>
        )}
      </header>
      <div className="space-y-1.5">
        {visibleItems.map((it, idx) => (
          <FactRow
            key={it.fact_id || `${factType}-${idx}`}
            item={it}
            hideAuthor={!!dedupAuthor}
            glossary={glossary}
          />
        ))}
      </div>
      {remaining > 0 && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className="mt-2 text-xs text-primary hover:underline"
        >
          Show {remaining} more
        </button>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function KeyFactsModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as KeyFactsData;
  const items = Array.isArray(data.items) ? data.items : [];
  const label =
    typeof data.label === "string" && data.label ? data.label : "Key Facts";
  const glossary =
    data.glossary && typeof data.glossary === "object"
      ? (data.glossary as GlossaryMap)
      : undefined;

  // Critical facts promote to a flat strip outside group nesting.
  const critical = items.filter((it) => severityFor(it) === "critical");
  const rest = items.filter((it) => severityFor(it) !== "critical");

  // Group remaining facts by fact_type. Preserve the catalog group
  // order when the data carries it; append unknown types at the end.
  const groupOrder: string[] =
    Array.isArray(data.groups) && data.groups.length > 0
      ? (data.groups as string[])
      : ["decision", "observation", "open_question", "action_item", "opinion"];
  const grouped = new Map<string, KeyFactItem[]>();
  for (const it of rest) {
    const key = (it.fact_type || "observation").toLowerCase();
    const arr = grouped.get(key);
    if (arr) arr.push(it);
    else grouped.set(key, [it]);
  }
  // Stable order: catalog order first, then any extra types alphabetically.
  const orderedGroupKeys: string[] = [];
  for (const k of groupOrder) {
    if (grouped.has(k)) orderedGroupKeys.push(k);
  }
  for (const k of Array.from(grouped.keys()).sort()) {
    if (!orderedGroupKeys.includes(k)) orderedGroupKeys.push(k);
  }

  if (critical.length === 0 && rest.length === 0) {
    return null;
  }

  return (
    <section
      className="mt-8"
      id={`module-${module.anchor}`}
      data-testid="module-key_facts"
    >
      <h2 className="text-lg font-semibold text-foreground mb-3">{label}</h2>
      <div data-toc-skip>
        {critical.length > 0 && (
          <div
            className="space-y-1.5 mb-4"
            data-testid="key-facts-critical-strip"
          >
            {critical.map((it, idx) => (
              <FactRow
                key={it.fact_id || `critical-${idx}`}
                item={it}
                hideAuthor={false}
                glossary={glossary}
              />
            ))}
          </div>
        )}
        {orderedGroupKeys.map((k) => (
          <GroupSection
            key={k}
            factType={k}
            items={grouped.get(k) || []}
            glossary={glossary}
          />
        ))}
      </div>
    </section>
  );
}
