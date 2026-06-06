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
/** Kinds whose "source" is a person+channel rather than a titled document. */
const MESSAGE_KINDS = new Set(["channel_message", "qa_history"]);

/**
 * A bare platform-native id (e.g. Slack "U0B55TPHLHF") that leaked through when a
 * display name couldn't be resolved. It carries no trust to a reader, so we omit
 * it rather than print it. Requires a U/W/C/D/G/T prefix, 8+ chars, AND at least
 * one digit — real platform ids always contain digits, so an all-caps display
 * name/handle like "TEAMWORK" or "CATHERINE" is never mistaken for an id.
 */
const RAW_ID_RE = /^[UWCDGT][A-Z0-9]*[0-9][A-Z0-9]{6,}$/;
function isRawPlatformId(s: string): boolean {
  return RAW_ID_RE.test(s.trim());
}

/** Registrable domain (no leading www.) of an http(s) url, or "" if unparseable. */
function domainOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./i, "");
  } catch {
    return "";
  }
}

/**
 * One concise, NAMED citation line as a canonical markdown list item. The numbered
 * marker `[N]` is itself the clickable link when a permalink is present
 * (`[N](url)`) — matching the inline `[N]` markers in the answer. After the marker
 * comes a human-readable label tuned per kind so a source reads at a glance
 * (Perplexity/Glean style) instead of a bare `[N]`:
 *   - channel_message → `{author} · {#channel} · {age}`
 *   - web_result      → `{domain} · {age}`
 *   - wiki_page       → `{page title} · {age}`
 *   - decision/graph  → `{short title} · {author?} · {age}`
 * A raw platform user-id is never shown as an author; segments are omitted when
 * absent, so a bare citation still renders `- {icon} [N]`.
 */
function renderCitationLine(c: Citation, num: number, platform: string): string {
  const url = c.url ? cleanUrl(c.url) : "";
  const marker = url ? `[${num}](${url})` : `[${num}]`;
  const author = c.author && !isRawPlatformId(c.author) ? cleanField(c.author, 80) : "";
  // The label for titled sources (wiki/decision/graph). `text` is an excerpt; a
  // real `title` is preferred so we don't show truncated garbage.
  const label = c.text ? cleanField(c.text, 80) : "";
  const age = c.timestamp ? (relativeTime(c.timestamp) ?? "") : "";

  const segments: string[] = [];
  if (MESSAGE_KINDS.has(c.type)) {
    // Who said it, where — the message text itself is referenced by the inline [N].
    if (author) segments.push(author);
    if (c.source) {
      const src = cleanField(c.source, 80);
      let chan = src.startsWith("#") ? src : `#${src}`; // idempotent hash
      // Cross-platform provenance: when this source came from a DIFFERENT
      // platform than the one being answered in, mark it (e.g. "#general (discord)").
      const srcPlatform = c.platform ? cleanField(c.platform, 20).toLowerCase() : "";
      if (srcPlatform && srcPlatform !== platform) chan += ` (${srcPlatform})`;
      segments.push(chan);
    }
  } else if (c.type === "web_result") {
    // The 🌐 icon already signals "web"; the domain is the label. Only fall back
    // to a "(web)" suffix on the title when neither icon-implied domain exists.
    segments.push(domainOf(url) || label);
  } else if (c.type === "wiki_page") {
    // Prefer the real page title; fall back to the excerpt ONLY if absent, and
    // cap that fallback harder (40) so an incomplete excerpt is obviously partial.
    const wikiLabel = c.title ? cleanField(c.title, 80) : (label ? cleanField(label, 40) : "");
    if (wikiLabel) segments.push(wikiLabel);
  } else {
    // decision_record / graph_relationship / media / uploaded_file
    if (label) segments.push(label);
    if (author) segments.push(author);
  }
  if (age) segments.push(age);

  const tail = segments.filter((s) => s.length > 0).join(" · ");
  const head = `- ${iconFor(c.type)} ${marker}`;
  return tail ? `${head} ${tail}` : head;
}

/**
 * Render one citation block under a heading. Entries carry their ORIGINAL
 * 1-based index so inline `[n]` markers in the answer stay valid even though
 * sources and related context are shown in separate blocks.
 */
function renderCitationBlock(
  heading: string,
  entries: Array<{ c: Citation; num: number }>,
  platform: string,
): string {
  if (entries.length === 0) return "";
  const shown = entries.slice(0, MAX_CITATIONS);
  const lines = shown.map(({ c, num }) => renderCitationLine(c, num, platform)).join("\n");
  const overflow = entries.length - shown.length;
  const more = overflow > 0 ? `\n_+${overflow} more_` : "";
  // Surface the total count in the heading when the list overflows, so the reader
  // knows how many sources back the answer (e.g. "## 📎 Sources · 9").
  const head = entries.length > MAX_CITATIONS ? `${heading} · ${entries.length}` : heading;
  return `\n\n${head}\n${lines}${more}`;
}

/** Backward-compatible: render all citations as a single Sources block. */
export function renderCitations(citations: Citation[], platform = "unknown"): string {
  return renderCitationBlock("## 📎 Sources", citations.map((c, i) => ({ c, num: i + 1 })), platform);
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
const MEDIUM_CONFIDENCE = 0.6;

/**
 * A subtle, banded confidence signal shown ONLY when the backend reports a real
 * score (`0` = "no signal" / older backend → silent; never fabricates a number):
 *   - ≤0.35 → a "low confidence, please verify" warning,
 *   - ≤0.60 → a softer "based on limited sources" nudge (so a medium answer
 *             doesn't read as authoritative),
 *   - >0.60 → silent (a confident answer needs no caveat).
 */
export function renderConfidence(confidence: number, isEmpty: boolean): string {
  if (isEmpty) return "";
  if (typeof confidence !== "number" || confidence <= 0) return "";
  if (confidence <= LOW_CONFIDENCE) return `\n⚠️ _low confidence — please verify against the sources_`;
  if (confidence <= MEDIUM_CONFIDENCE) return `\nℹ️ _based on limited sources — worth a double-check_`;
  return "";
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

/** Whole days between an ISO timestamp and `now`; null if unparseable. */
export function ageInDays(iso: string, now: number = Date.now()): number | null {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.floor((now - t) / 86_400_000);
}

/** Beyond this age, the freshness footer adds a "may be outdated" caveat. */
const STALE_AFTER_DAYS = 30;

/**
 * Honest freshness footer. The backend's `last_sync_ts` is the timestamp of the
 * channel's last synced *message*, not the sync-run time, so we label it "last
 * activity" — saying "synced Nd ago" misled users on quiet-but-fresh channels.
 * When that activity is older than {@link STALE_AFTER_DAYS} days the line gains a
 * soft "may be outdated" caveat so a stale answer doesn't read as current.
 */
function renderFreshness(lastSyncTs?: string): string {
  if (!lastSyncTs) return "";
  const rel = relativeTime(lastSyncTs);
  if (!rel) return "";
  const days = ageInDays(lastSyncTs);
  if (days !== null && days > STALE_AFTER_DAYS) {
    return `\n🕐 _last activity ${rel} — may be outdated_`;
  }
  return `\n🕐 _last activity ${rel}_`;
}

/**
 * Internal agent/route names that must never be shown to end users — "via
 * qa_agent" is developer chrome, not information. The footer only renders for a
 * genuinely user-facing route name (none today), so it effectively disappears.
 */
const INTERNAL_ROUTES = new Set(["qa_agent", "deep", "quick", "summarize", "memory_agent", "router", "echo"]);
function renderRoute(route: string): string {
  if (!route || INTERNAL_ROUTES.has(route)) return "";
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

  const citations = result.citations || [];
  const { sources, related } = partitionCitations(citations);
  // "No sources → no citation UI" (Glean rule): a greeting or general-knowledge
  // reply with zero citations must NOT show a confidence caveat ("verify against
  // the sources" with no sources), a Related block, or the route footer. It
  // renders as just the answer + follow-ups.
  const hasSources = sources.length + related.length > 0;
  // Channel freshness ("last activity Nd ago") only makes sense when the answer
  // actually rests on channel messages — not for web/wiki-only answers.
  const hasChannelSource = citations.some((c) => MESSAGE_KINDS.has(c.type));

  const body =
    (result.answer || "").trimEnd() +
    // A low-confidence warning sits right under the answer so truncation can
    // never drop this trust signal — but only when there ARE sources to verify.
    (hasSources ? renderConfidence(result.confidence, result.isEmpty) : "") +
    renderCitationBlock("## 📎 Sources", sources, plat) +
    renderCitationBlock("## 🧠 Related", related, plat) +
    renderTensions(result.tensions) +
    (hasChannelSource ? renderFreshness(result.lastSyncTs) : "") +
    renderFollowUps(result.followUps) +
    (hasSources ? renderRoute(result.route || "") : "");

  return enforceCap(body, capFor(plat));
}
