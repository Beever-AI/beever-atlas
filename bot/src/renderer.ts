/**
 * Platform-aware reply renderer.
 *
 * Replaces the old flat `formatBlockKit` string for ALL platforms. Given an
 * assembled `AskResult` and the originating platform, it produces a single
 * markdown body that:
 *   - leads with the answer,
 *   - lists up to {@link MAX_CITATIONS} sources with a kind icon + provenance,
 *   - shows channel freshness when known,
 *   - appends a subtle route footer,
 *   - is hard-capped to the platform's message length so the adapter never
 *     rejects an over-long post.
 *
 * When `result.isEmpty` is set it renders an honest, actionable empty state
 * instead of surfacing an LLM "I couldn't find anything" essay.
 *
 * OUTPUT CONTRACT: this returns ONE canonical GitHub-flavored CommonMark string
 * — `## headings`, `[label](url)` links, `- ` bullets, `**bold**`, `_italic_`.
 * The caller posts it as `{ markdown }`, so the chat SDK's per-platform
 * FormatConverter owns the native conversion (Slack demotes `##`→bold and
 * `[x](u)`→`<u|x>`, Teams→Adaptive Card text, Discord/Mattermost render natively,
 * Telegram→MarkdownV2). DIRECTIVE: do NOT re-introduce platform-specific syntax
 * here (bare `<url>` autolinks, Slack `*bold*`, `• ` bullets) — emit portable
 * markdown only and let the SDK convert. Tables/blockquotes are intentionally
 * not emitted (uneven cross-platform support). Native cards / interactive
 * buttons remain a deliberate follow-up (they need the SDK `onAction` webhook).
 */

import type { AskResult, Citation, Tension } from "./types.js";

/**
 * Per-platform max message length (chars), set conservatively below each
 * platform's true ceiling so we truncate before the adapter would reject.
 * `unknown` is the safe default for unrecognized thread-id prefixes.
 */
export const CHAR_CAP: Record<string, number> = {
  slack: 3900,
  discord: 1950,
  teams: 27000,
  telegram: 4000,
  mattermost: 16000,
  unknown: 3500,
};

const MAX_CITATIONS = 5;
const TRUNCATE_SUFFIX = "\n…_[truncated]_";

/** Icon per citation kind so wiki / message / decision / graph sources read at a glance. */
const KIND_ICON: Record<string, string> = {
  wiki_page: "📖",
  channel_message: "💬",
  qa_history: "💬",
  decision_record: "⚖️",
  graph_relationship: "🧠",
  media: "🖼️",
  uploaded_file: "📎",
  web_result: "🌐",
};

function iconFor(kind: string): string {
  return KIND_ICON[kind] ?? "•";
}

function capFor(platform: string): number {
  return CHAR_CAP[platform] ?? CHAR_CAP.unknown;
}

/** Truncate to a hard char budget, appending a marker when content was cut. */
export function enforceCap(text: string, cap: number): string {
  if (text.length <= cap) return text;
  const budget = Math.max(0, cap - TRUNCATE_SUFFIX.length);
  let head = text.slice(0, budget);
  // Don't slice through a surrogate pair — a lone high surrogate renders as a
  // replacement character on every platform.
  const lastCode = head.charCodeAt(head.length - 1);
  if (lastCode >= 0xd800 && lastCode <= 0xdbff) head = head.slice(0, -1);
  const out = head.trimEnd() + TRUNCATE_SUFFIX;
  // Guarantee the contract even when the cap is smaller than the marker itself.
  return out.length <= cap ? out : text.slice(0, cap);
}

/**
 * Defense-in-depth: citation fields come from the backend but pass through
 * channel-message content, wiki titles, and user display names. Collapse
 * control chars / newlines (so a crafted value can't forge an extra
 * "## 📎 Sources" heading or break the layout) and bound the length.
 */
function cleanField(s: string, max = 300): string {
  let out = "";
  for (const ch of s) {
    const code = ch.codePointAt(0) ?? 0;
    out += code < 0x20 || code === 0x7f ? " " : ch;
  }
  return out.replace(/\s+/g, " ").trim().slice(0, max);
}

/**
 * Only emit http(s) links, stripped of anything that could break a markdown
 * `[label](url)` link target — whitespace, angle brackets, and parentheses.
 * (Real chat/wiki permalinks don't contain raw parens; dropping a rare one is
 * safer than emitting a link the markdown parser would mangle.)
 */
function cleanUrl(u: string): string {
  const t = u.trim();
  if (!/^https?:\/\//i.test(t)) return "";
  return t.replace(/[\s<>()]+/g, "");
}

/** Citation kinds that read as "related context" (graph) rather than direct sources. */
const RELATED_KINDS = new Set(["decision_record", "graph_relationship"]);

/**
 * One concise citation line as a canonical markdown list item:
 *   `- {icon} [N] {author} · {#channel} · [open](url)`
 *
 * The full fact text is intentionally dropped — the inline `[N]` marker in the
 * answer already references it, and repeating the whole fact here buried the
 * answer under a wall of sources (live-test finding: "sources too verbose").
 * Segments are omitted when absent, so a bare citation still renders
 * `- {icon} [N]`. `[open](url)` is a real markdown link the SDK converts to
 * each platform's native link syntax.
 */
function renderCitationLine(c: Citation, num: number): string {
  const segments: string[] = [];
  if (c.author) segments.push(cleanField(c.author, 80));
  if (c.source) segments.push(cleanField(c.source, 80));
  const url = c.url ? cleanUrl(c.url) : "";
  if (url) segments.push(`[open](${url})`);
  const tail = segments.filter((s) => s.length > 0).join(" · ");
  const head = `- ${iconFor(c.type)} [${num}]`;
  return tail ? `${head} ${tail}` : head;
}

/**
 * Render one citation block under a heading. Entries carry their ORIGINAL
 * 1-based index so inline `[n]` markers in the answer stay valid even though
 * sources and related context are shown in separate blocks.
 */
function renderCitationBlock(heading: string, entries: Array<{ c: Citation; num: number }>): string {
  if (entries.length === 0) return "";
  const shown = entries.slice(0, MAX_CITATIONS);
  const lines = shown.map(({ c, num }) => renderCitationLine(c, num)).join("\n");
  const overflow = entries.length - shown.length;
  const more = overflow > 0 ? `\n_+${overflow} more_` : "";
  return `\n\n${heading}\n${lines}${more}`;
}

/** Backward-compatible: render all citations as a single Sources block. */
export function renderCitations(citations: Citation[]): string {
  return renderCitationBlock("## 📎 Sources", citations.map((c, i) => ({ c, num: i + 1 })));
}

/** Group citations into direct sources vs graph "related context", keeping indices. */
export function partitionCitations(citations: Citation[]): {
  sources: Array<{ c: Citation; num: number }>;
  related: Array<{ c: Citation; num: number }>;
} {
  const sources: Array<{ c: Citation; num: number }> = [];
  const related: Array<{ c: Citation; num: number }> = [];
  citations.forEach((c, i) => {
    (RELATED_KINDS.has(c.type) ? related : sources).push({ c, num: i + 1 });
  });
  return { sources, related };
}

const LOW_CONFIDENCE = 0.35;

/**
 * A subtle low-confidence warning — shown ONLY when the backend reports a real
 * score at/below the threshold. `0` means "no signal" (older backend) → silent;
 * a high score → silent. Never fabricates a number.
 */
export function renderConfidence(confidence: number, isEmpty: boolean): string {
  if (isEmpty) return "";
  if (typeof confidence !== "number" || confidence <= 0 || confidence > LOW_CONFIDENCE) return "";
  return `\n⚠️ _low confidence — please verify against the sources_`;
}

/** Proactive "heads up" when the answer touches a documented tension (max 2). */
export function renderTensions(tensions?: Tension[]): string {
  if (!tensions || tensions.length === 0) return "";
  const items = tensions
    .slice(0, 2)
    .map((t) => {
      const title = cleanField(t.title, 140);
      const detail = t.detail ? cleanField(t.detail, 160) : "";
      return detail ? `- ${title} — ${detail}` : `- ${title}`;
    })
    .filter((l) => l.length > 2);
  if (items.length === 0) return "";
  return `\n\n**⚠️ Heads up — possible tension**\n${items.join("\n")}`;
}

/** Human-friendly "Xm/Xh/Xd ago" from an ISO timestamp; null if unparseable. */
export function relativeTime(iso: string, now: number = Date.now()): string | null {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const diffMs = now - t;
  // Future timestamps (clock skew) collapse to "just now" rather than showing a
  // misleading "in N hours".
  if (diffMs < 60_000) return "just now";
  const min = Math.floor(diffMs / 60_000);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const days = Math.floor(hr / 24);
  return `${days}d ago`;
}

/**
 * Honest freshness footer. The backend's `last_sync_ts` is the timestamp of the
 * channel's last synced *message*, not the sync-run time, so we label it "last
 * activity" — saying "synced Nd ago" misled users on quiet-but-fresh channels.
 */
function renderFreshness(lastSyncTs?: string): string {
  if (!lastSyncTs) return "";
  const rel = relativeTime(lastSyncTs);
  return rel ? `\n🕐 _last activity ${rel}_` : "";
}

function renderRoute(route: string): string {
  return `\n_via ${cleanField(route, 40)}_`;
}

/**
 * Suggested related questions as a short "You might also ask" list (max 3).
 * Rendered before the route footer so that, under truncation, the chips are the
 * sacrificial tail and the answer is always preserved.
 */
export function renderFollowUps(followUps?: string[]): string {
  if (!followUps || followUps.length === 0) return "";
  const items = followUps
    .map((q) => cleanField(q, 120))
    .filter((q) => q.length > 0)
    .slice(0, 3);
  if (items.length === 0) return "";
  return `\n\n_You might also ask:_\n${items.map((q) => `- ${q}`).join("\n")}`;
}

/**
 * Honest, actionable empty state — replaces the old wall of
 * "I could not find any indexed memories…" text. Short, and points to the
 * next step instead of dead-ending.
 */
export function renderEmptyState(result: AskResult, platform: string): string {
  const lines = [
    "I don't have anything indexed for that in this channel yet.",
    "",
    "- The channel may not be synced yet — an admin can trigger a sync.",
    "- Try rephrasing, or ask in a channel that's already indexed.",
  ];
  const freshness = renderFreshness(result.lastSyncTs);
  if (freshness) lines.push(freshness.trimStart());
  return enforceCap(lines.join("\n"), capFor(platform));
}

/** Render a full reply for the given platform. */
export function renderResponse(result: AskResult, platform: string): string {
  const plat = (platform || "unknown").toLowerCase();
  if (result.isEmpty) return renderEmptyState(result, plat);

  const { sources, related } = partitionCitations(result.citations || []);
  const body =
    (result.answer || "").trimEnd() +
    // A low-confidence warning sits right under the answer so truncation can
    // never drop this trust signal.
    renderConfidence(result.confidence, result.isEmpty) +
    renderCitationBlock("## 📎 Sources", sources) +
    renderCitationBlock("## 🧠 Related", related) +
    renderTensions(result.tensions) +
    renderFreshness(result.lastSyncTs) +
    renderFollowUps(result.followUps) +
    renderRoute(result.route || "qa_agent");

  return enforceCap(body, capFor(plat));
}
