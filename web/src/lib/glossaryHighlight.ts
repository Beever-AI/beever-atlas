/**
 * Inline glossary tooltips for body text.
 *
 * The wiki's ``acronym_legend`` module already shows definitions at
 * the bottom of the page, but readers had to scroll to find them.
 * This helper post-processes rendered text nodes so glossary terms
 * (e.g. "MFA", "RAG", "SAML") get a dotted-underline + native
 * ``title=`` tooltip the moment they appear in body copy.
 *
 * Implementation notes:
 *  - We use the native ``title`` attribute (zero JS runtime cost,
 *    works without any tooltip library).
 *  - The regex is built once per call so callers don't pay
 *    per-acronym scan cost — performance is O(text-length × regex)
 *    not O(text-length × terms).
 *  - The output is a React node array; existing inline elements
 *    (anchors from ``linkifyText``) pass through untouched. We only
 *    walk plain string entries.
 */

import { createElement, type ReactNode } from "react";

export type GlossaryMap = Record<string, string>;

/** Escape regex metacharacters in a glossary term so user-provided
 *  acronyms can safely become regex alternatives. */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Build a single regex that matches ANY glossary term as a whole
 *  word. Returns ``null`` when the glossary is empty so callers can
 *  short-circuit. Sorted longest-first so multi-word entries beat
 *  shorter substrings (e.g. "Open ID Connect" matches before "Open"). */
function buildGlossaryRegex(glossary: GlossaryMap): RegExp | null {
  const terms = Object.keys(glossary)
    .map((t) => t.trim())
    .filter((t) => t.length > 0)
    .sort((a, b) => b.length - a.length);
  if (terms.length === 0) return null;
  const pattern = terms.map(escapeRegExp).join("|");
  // Word-boundary on both sides so we don't match "MFA" inside
  // "MFAuthentication" or similar. The ``g`` flag is essential — we
  // call ``regex.exec`` in a loop and rely on lastIndex bookkeeping.
  return new RegExp(`\\b(?:${pattern})\\b`, "g");
}

/** Wrap glossary-term occurrences inside a single string with
 *  ``<span title="definition" ...>`` nodes; non-matching segments
 *  pass through as plain strings. */
function highlightString(
  text: string,
  glossary: GlossaryMap,
  regex: RegExp,
  keyPrefix: string,
): ReactNode[] {
  if (!text) return [text];
  // Build a case-insensitive lookup once so "MFA" and "mfa" both
  // resolve to the same definition. We keep the matched casing in
  // the output so the source text isn't visually rewritten.
  const lookup = new Map<string, string>();
  for (const [term, def] of Object.entries(glossary)) {
    lookup.set(term.toLowerCase(), def);
  }
  const out: ReactNode[] = [];
  regex.lastIndex = 0;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let counter = 0;
  while ((match = regex.exec(text)) !== null) {
    const start = match.index;
    const matched = match[0];
    const definition = lookup.get(matched.toLowerCase());
    if (start > lastIndex) {
      out.push(text.slice(lastIndex, start));
    }
    if (definition) {
      out.push(
        createElement(
          "span",
          {
            key: `${keyPrefix}-gl-${counter}-${start}`,
            title: definition,
            className:
              "border-b border-dotted border-muted-foreground/60 cursor-help",
            "data-glossary-term": matched,
          },
          matched,
        ),
      );
    } else {
      // Glossary entry exists with empty definition — pass through.
      out.push(matched);
    }
    lastIndex = start + matched.length;
    counter += 1;
  }
  if (lastIndex < text.length) {
    out.push(text.slice(lastIndex));
  }
  return out;
}

/**
 * Walk an array of React nodes (the output of ``linkifyText``) and
 * apply glossary highlighting to plain string entries. Non-string
 * entries (e.g. ``<a>`` tags from URL linkification) pass through
 * unchanged so we don't disturb already-rendered inline elements.
 *
 * Returns the input array unchanged when the glossary is empty/null.
 */
export function applyGlossaryToNodes(
  nodes: ReactNode[],
  glossary: GlossaryMap | undefined | null,
  keyPrefix = "g",
): ReactNode[] {
  if (!glossary) return nodes;
  const regex = buildGlossaryRegex(glossary);
  if (!regex) return nodes;
  const out: ReactNode[] = [];
  let i = 0;
  for (const node of nodes) {
    if (typeof node === "string") {
      const segment = highlightString(node, glossary, regex, `${keyPrefix}-${i}`);
      out.push(...segment);
    } else {
      out.push(node);
    }
    i += 1;
  }
  return out;
}
