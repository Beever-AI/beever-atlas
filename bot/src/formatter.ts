/**
 * Slack Block Kit response formatter.
 * Formats agent responses as rich Slack messages with answer, citations, and route badge.
 */

interface Citation {
  type: string;
  text: string;
}

const ROUTE_EMOJI: Record<string, string> = {
  semantic: ":brain:",
  graph: ":spider_web:",
  both: ":zap:",
  echo: ":loud_sound:",
};

export function formatBlockKit(
  answer: string,
  citations: Citation[],
  route: string,
): string {
  const sections: string[] = [];

  // Answer section
  sections.push(answer);

  // Citations section (if any)
  if (citations.length > 0) {
    const citationLines = citations
      .map((c, i) => `[${i + 1}] ${c.text}`)
      .join("\n");
    sections.push(`\n---\n*Sources:*\n${citationLines}`);
  }

  // Route badge
  const emoji = ROUTE_EMOJI[route] || ":robot_face:";
  sections.push(`\n${emoji} _Route: ${route}_`);

  return sections.join("\n");
}
