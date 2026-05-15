/** Parse an LLM-generated wiki Overview body into typed sections so
 *  the OverviewPage can render high-value sections via dedicated
 *  React components (cards, grids) instead of raw markdown bullets.
 *
 *  Input: raw markdown content (the post-h1-stripped body).
 *  Output: a struct with extracted sections + the residual body that
 *  should still be rendered by WikiMarkdown for whatever the parser
 *  didn't recognise.
 *
 *  The parser is intentionally tolerant — if a section heading is
 *  named slightly differently or the bullet shape varies, the
 *  fallback is to leave the markdown in the residual body and let
 *  WikiMarkdown render it as today. No information is lost.
 */

import type { ContributorGroup } from "@/components/wiki/ContributorCard";
import type { ToolEntry } from "@/components/wiki/ToolCard";

export interface ParsedOverview {
  /** Bold one-sentence TL;DR pulled from the top of the body, if
   *  present. Heuristic: a markdown line wholly bolded with `**...**`
   *  in the first 3 lines of the body. */
  tldr: string;
  /** The intro paragraph(s) before any ## section heading. */
  intro: string;
  /** Mermaid block extracted from the Concept Map section, if any. */
  conceptMapMermaid: string;
  /** Structured Key Contributors with optional grouping. Empty list
   *  means the parser couldn't find or recognise the section — the
   *  residual body still contains it for WikiMarkdown fallback. */
  contributors: ContributorGroup[];
  /** Structured Tools & Resources entries. */
  tools: ToolEntry[];
  /** Markdown that wasn't extracted — render via WikiMarkdown. */
  residualBody: string;
}

function _splitSections(content: string): { intro: string; sections: Array<{ heading: string; body: string }> } {
  const lines = content.split("\n");
  const intro: string[] = [];
  const sections: Array<{ heading: string; body: string }> = [];
  let current: { heading: string; body: string[] } | null = null;
  for (const line of lines) {
    const m = line.match(/^##\s+(.+?)\s*$/);
    if (m) {
      if (current) {
        sections.push({ heading: current.heading, body: current.body.join("\n").trim() });
      } else {
        // Lines accumulated before any ## go into intro.
        if (intro.length > 0 && intro.join("").trim()) {
          // intro is already accumulated; do nothing.
        }
      }
      current = { heading: m[1].trim(), body: [] };
    } else if (current) {
      current.body.push(line);
    } else {
      intro.push(line);
    }
  }
  if (current) {
    sections.push({ heading: current.heading, body: current.body.join("\n").trim() });
  }
  return { intro: intro.join("\n").trim(), sections };
}

function _extractTldr(intro: string): { tldr: string; rest: string } {
  // Look at the first 3 non-empty lines for a bold-only line.
  const lines = intro.split("\n");
  const out: string[] = [];
  let tldr = "";
  let foundCount = 0;
  for (const line of lines) {
    const t = line.trim();
    if (t.length === 0) {
      out.push(line);
      continue;
    }
    foundCount += 1;
    const m = t.match(/^\*\*(.+)\*\*\.?$/);
    if (!tldr && m && foundCount <= 3) {
      tldr = m[1].trim();
      // Don't push this line into out — it's been promoted to TL;DR.
      continue;
    }
    out.push(line);
  }
  return { tldr, rest: out.join("\n").trim() };
}

function _extractMermaidBlock(body: string): { mermaid: string; rest: string } {
  // Match a ```mermaid ... ``` fenced block (capture inner text).
  const re = /```mermaid\s*\n([\s\S]*?)```/m;
  const m = body.match(re);
  if (!m) return { mermaid: "", rest: body };
  const before = body.slice(0, m.index ?? 0);
  const after = body.slice((m.index ?? 0) + m[0].length);
  return { mermaid: m[1].trim(), rest: (before + after).trim() };
}

function _parseContributors(body: string): ContributorGroup[] {
  // Two patterns the LLM emits for grouped contributors:
  //   - **Group Name**            (a bold-only bullet → group label)
  //   - Person Name: contribution [N] [M]
  // Within a group, each subsequent non-bold bullet is a person.
  // When no group bullets exist, all entries land in a single
  // un-titled group.
  const lines = body.split("\n");
  const groups: ContributorGroup[] = [{ group: "", entries: [] }];
  let current = groups[0];

  const groupRe = /^\s*-\s*\*\*(.+?)\*\*\s*$/;
  const personRe = /^\s*-\s*(?:\*\*)?(.+?)(?:\*\*)?\s*[:—–-]\s*(.+?)$/;
  const citationRe = /\[(\d+)\]/g;

  for (const line of lines) {
    const groupMatch = line.match(groupRe);
    if (groupMatch) {
      const label = groupMatch[1].trim();
      // Heuristic: short label without colon/period is a group;
      // longer prose with citations is a person whose entire bullet
      // happens to be bolded (rare).
      if (label.length < 80 && !label.includes(":") && !/\[\d+\]/.test(label)) {
        if (current.entries.length === 0 && groups.length === 1) {
          current.group = label;
        } else {
          current = { group: label, entries: [] };
          groups.push(current);
        }
        continue;
      }
    }
    const personMatch = line.match(personRe);
    if (personMatch) {
      const name = personMatch[1].replace(/\*\*/g, "").trim();
      let contributionRaw = personMatch[2].trim();
      // Pull out citations + leftover role parens (e.g., "(expert)").
      const citations: number[] = [];
      let m: RegExpExecArray | null;
      while ((m = citationRe.exec(contributionRaw))) {
        citations.push(parseInt(m[1], 10));
      }
      // Strip the citation chips from contribution text — we render
      // them as their own pills next to the name.
      contributionRaw = contributionRaw.replace(/\[\d+\]/g, "").trim();
      // Split off a trailing parenthetical role (e.g. "(expert)") if
      // the text doesn't carry it as a real role.
      let role = "";
      const roleMatch = contributionRaw.match(/^\((.*?)\)\s*(.*)$/);
      if (roleMatch) {
        role = roleMatch[1].trim();
        contributionRaw = roleMatch[2].trim();
      }
      if (name.length > 0 && name.length < 80) {
        current.entries.push({
          name,
          role,
          contribution: contributionRaw,
          citations,
        });
      }
    }
  }

  // Drop empty groups + return.
  return groups.filter((g) => g.entries.length > 0);
}

function _parseTools(body: string): ToolEntry[] {
  const lines = body.split("\n");
  const out: ToolEntry[] = [];
  // Pattern: - **Name** — description, OR - Name: description, OR - Name — description
  const re = /^\s*-\s*(?:\*\*)?(.+?)(?:\*\*)?\s*[:—–-]\s*(.+?)$/;
  for (const line of lines) {
    const m = line.match(re);
    if (!m) continue;
    const name = m[1].replace(/\*\*/g, "").trim();
    const desc = m[2].replace(/\[\d+\]/g, "").trim();
    if (name.length > 0 && name.length < 60) {
      out.push({ name, description: desc });
    }
  }
  return out;
}

function _matchesHeading(heading: string, candidates: string[]): boolean {
  const h = heading.toLowerCase().trim();
  return candidates.some((c) => h === c || h.includes(c));
}

export function parseOverviewBody(content: string): ParsedOverview {
  const { intro: rawIntro, sections } = _splitSections(content);
  const { tldr, rest: introRest } = _extractTldr(rawIntro);

  let conceptMapMermaid = "";
  let contributors: ContributorGroup[] = [];
  let tools: ToolEntry[] = [];
  const residualSections: Array<{ heading: string; body: string }> = [];

  // Sections to ALWAYS drop from the rendered body — the data is
  // already shown elsewhere (chips at the top, dedicated card grids,
  // freshness indicator) so re-rendering them as flat markdown
  // bullets is duplicate noise. Defensive: handles legacy pages
  // generated before the OVERVIEW_PROMPT dropped these sections.
  const _DROP_HEADINGS = [
    "topics at a glance",  // duplicates the React TopicCard grid
    "recent momentum",     // duplicates freshness chip + (future) activity feed
    "key highlights",      // duplicates the metadata chip row (memories / folders / topics / updated)
  ];

  for (const sec of sections) {
    if (_matchesHeading(sec.heading, _DROP_HEADINGS)) {
      // Silent drop — content is shown elsewhere on the page.
      continue;
    }
    if (_matchesHeading(sec.heading, ["concept map", "concept", "conceptmap"])) {
      const { mermaid, rest } = _extractMermaidBlock(sec.body);
      conceptMapMermaid = mermaid;
      // If the section had prose around the mermaid, keep that prose.
      if (rest.trim()) {
        residualSections.push({ heading: sec.heading, body: rest });
      }
    } else if (_matchesHeading(sec.heading, ["key contributors", "contributors", "people"])) {
      contributors = _parseContributors(sec.body);
      // If parsing failed (zero entries), keep section as residual.
      if (contributors.length === 0 && sec.body.trim()) {
        residualSections.push(sec);
      }
    } else if (
      _matchesHeading(sec.heading, ["tools & resources", "tools and resources", "tools", "tools/resources"])
    ) {
      tools = _parseTools(sec.body);
      if (tools.length === 0 && sec.body.trim()) {
        residualSections.push(sec);
      }
    } else {
      residualSections.push(sec);
    }
  }

  // Reassemble residual markdown for the WikiMarkdown fallback.
  const residualParts: string[] = [];
  for (const sec of residualSections) {
    residualParts.push(`## ${sec.heading}\n\n${sec.body}`);
  }
  return {
    tldr,
    intro: introRest,
    conceptMapMermaid,
    contributors,
    tools,
    residualBody: residualParts.join("\n\n").trim(),
  };
}
