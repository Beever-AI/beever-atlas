import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { type ReactNode, Fragment, useState } from "react";
import { FileText, ExternalLink, Link2, Image as ImageIcon, Film, X, Maximize2 } from "lucide-react";
import { MermaidBlock } from "./MermaidBlock";
import { ChartBlock } from "./ChartBlock";
import { CalloutBox } from "./CalloutBox";
import { CitationLink } from "./CitationLink";
import { WikiLink } from "./WikiLink";
import { buildLoaderUrl } from "@/lib/api";
import { ProxiedImage } from "@/components/common/ProxiedImage";
import { filesProxyPathFor } from "@/lib/mediaUrl";
import type { WikiCitation } from "@/lib/types";

/** Returns the route path needed to mint a signed loader token, or null
 * if the URL is public (no proxy needed). Detection lives in
 * `lib/mediaUrl.ts` so MediaModal / wiki / entity-graph share one
 * allowlist (Slack, Mattermost cloud + self-hosted, Discord CDN).
 *
 * Hostname comparison is parsed-URL based, never substring — substring
 * matching against the full URL would treat `evil.com/files.slack.com`
 * as a Slack file. CodeQL alert #20. */
function proxyPathFor(url: string): string | null {
  return filesProxyPathFor(url) ?? null;
}

/** Synchronous fallback (raw key) for `<a href>` / `<iframe src>` cases
 * that cannot await an async mint. Issue #89 migration follow-up tracked
 * separately. */
function proxyUrl(url: string): string {
  const p = proxyPathFor(url);
  return p ? buildLoaderUrl(p) : url;
}

function detectMediaType(url: string, alt?: string): "image" | "pdf" | "video" | "link" {
  const lower = (url + " " + (alt || "")).toLowerCase();
  if (lower.match(/\.(png|jpg|jpeg|gif|webp|svg)(\?|$)/)) return "image";
  // Prefer the alt-text emoji marker so backend renderers can hint
  // the media kind even when the URL is opaque (Mattermost ID-only
  // file URLs have no extension).
  if ((alt || "").startsWith("📄") || lower.match(/\.(pdf)(\?|$)/) || lower.includes("pdf")) return "pdf";
  // Parse the URL once; falls back to "" so the lower-case substring
  // checks below still run on opaque/relative inputs. The hostname
  // anchored check (host === / endsWith ".host") closes the
  // ``codeql:js/incomplete-url-substring-sanitization`` finding —
  // ``"youtube.com" in url`` would otherwise match
  // ``https://attacker.example/?youtube.com``.
  let host = "";
  try { host = new URL(url, window.location.href).hostname.toLowerCase(); } catch { /* invalid URL */ }
  const isYouTube = host === "youtube.com" || host.endsWith(".youtube.com") || host === "youtu.be";
  const isVimeo = host === "vimeo.com" || host.endsWith(".vimeo.com");
  if ((alt || "").startsWith("🎥") || lower.match(/\.(mp4|mov|webm|avi)(\?|$)/) || lower.includes("video") || isYouTube || isVimeo) return "video";
  if ((alt || "").startsWith("🖼") || lower.includes("image")) return "image";
  return "link";
}

interface WikiMarkdownProps {
  content: string;
  citations?: WikiCitation[];
  onNavigate?: (pageId: string) => void;
  /** Resolved `[[wikilink]]` references for this page — `{title: slug}`.
   * Resolution happens server-side at apply_update time; the renderer
   * just looks up the title and emits an anchor.  Undefined / empty
   * means every bracketed reference falls into the broken-link path. */
  crossLinks?: Record<string, string>;
  /** Titles inside `[[...]]` that did NOT resolve. The renderer styles
   * them with a red dashed underline and clicking opens a "create
   * page?" affordance. */
  crossLinksBroken?: string[];
}

/**
 * Pre-process content: replace [N] citations with markers that survive markdown parsing.
 * Entity chips ($tech, @person) are removed — rendered as plain text for readability.
 * Also wraps bare chart JSON (emitted by LLM without code fences) into ```chart blocks.
 */
function preprocessContent(content: string): string {
  // Split on code fences so we never touch content that is already inside one.
  // Code fences start with ``` (optionally with a language tag) on their own line.
  // We alternate between "outside fence" and "inside fence" segments.
  const segments = content.split(/(^```[^\n]*\n[\s\S]*?^```)/gm);

  const processed = segments.map((seg, i) => {
    // Even-indexed segments are outside code fences; odd-indexed are inside.
    if (i % 2 !== 0) return seg; // already inside a fence — leave untouched

    // Wrap any bare chart JSON lines that are NOT already fenced.
    return seg.replace(
      /^[ \t]*(\{[^\n]*?"type"\s*:\s*"(?:bar|area|donut|pie)"[^\n]*?\})[ \t]*$/gm,
      (_, json) => "```chart\n" + json.trim() + "\n```"
    );
  });

  let result = processed.join("");

  // [[Page Title]] wikilinks \u2192 marker. MUST run before the citation
  // pass so a numeric-only title (rare but legal) doesn't get eaten by
  // the [N] regex.  ``encodeURIComponent`` is used as the in-band
  // escape so titles can carry colons / brackets / unicode without
  // colliding with the marker delimiter.
  result = result.replace(/\[\[([^\[\]\n]+?)\]\]/g, (_match, title: string) => {
    const trimmed = title.trim();
    if (!trimmed) return _match;
    return `\u200Bwl:${encodeURIComponent(trimmed)}\u200B`;
  });

  // Replace comma-separated citations like [1, 3, 13] with individual markers
  result = result.replace(/\[([\d,\s]+)\]/g, (_match, inner: string) => {
    const nums = inner.split(",").map(s => s.trim()).filter(s => /^\d+$/.test(s));
    if (nums.length === 0) return _match;
    return nums.map(n => `\u200Bcite:${n}\u200B`).join(" ");
  });
  // Also catch any remaining single [N] that weren't part of comma lists
  result = result.replace(/\[(\d+)\]/g, "\u200Bcite:$1\u200B");
  return result;
}

interface MarkerContext {
  citations: WikiCitation[];
  crossLinks: Record<string, string>;
  crossLinksBroken: Set<string>;
}

/**
 * Process text to render citation + wikilink markers as components.
 *
 * Pattern union \u2014 order does NOT matter for correctness because the
 * markers carry distinct prefixes (`cite:` vs `wl:`); we run both in
 * one pass to preserve key uniqueness across the original text span.
 */
function processText(
  text: string,
  ctx: MarkerContext,
  keyPrefix: string,
): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /\u200B(cite:(\d+)|wl:([^\u200B]+))\u200B/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    if (match[2] !== undefined) {
      const idx = parseInt(match[2], 10);
      parts.push(
        <CitationLink
          key={`${keyPrefix}-c-${match.index}`}
          index={idx}
          citation={ctx.citations[idx - 1]}
        />,
      );
    } else if (match[3] !== undefined) {
      const title = decodeURIComponent(match[3]);
      const slug = ctx.crossLinks[title];
      const isBroken = !slug || ctx.crossLinksBroken.has(title);
      parts.push(
        <WikiLink
          key={`${keyPrefix}-wl-${match.index}`}
          title={title}
          slug={isBroken ? undefined : slug}
        />,
      );
    }
    lastIndex = pattern.lastIndex;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return parts;
}

/**
 * Walk React children tree and process all text nodes for citations + wikilinks.
 */
function processChildren(
  children: ReactNode,
  ctx: MarkerContext,
  keyPrefix = "c",
): ReactNode {
  if (typeof children === "string") {
    const parts = processText(children, ctx, keyPrefix);
    return parts.length === 1 && typeof parts[0] === "string" ? parts[0] : <>{parts}</>;
  }
  if (Array.isArray(children)) {
    return <>{children.map((child, i) => processChildren(child, ctx, `${keyPrefix}-${i}`))}</>;
  }
  if (children && typeof children === "object" && "props" in (children as object)) {
    const el = children as React.ReactElement<{ children?: ReactNode }>;
    if (el.props?.children != null) {
      const processed = processChildren(el.props.children, ctx, `${keyPrefix}-el`);
      const { children: _, ...rest } = el.props;
      return { ...el, props: { ...rest, children: processed } };
    }
  }
  return children;
}

/**
 * Check if a ReactNode tree contains only CitationLink components and whitespace.
 */
function isCitationOnlyNode(node: ReactNode): boolean {
  if (node == null || node === false || node === true) return true;
  if (typeof node === "string") return node.trim() === "";
  if (typeof node === "number") return false;
  if (Array.isArray(node)) return node.every(isCitationOnlyNode);
  if (typeof node === "object" && "props" in (node as object)) {
    const el = node as React.ReactElement<{ children?: ReactNode }>;
    const type = (el as unknown as { type: unknown }).type;
    // CitationLink component — this is a citation
    if (type === CitationLink) return true;
    // Fragment or span wrapper — check children
    if (typeof type === "string" || typeof type === "symbol" || type === undefined) {
      return el.props?.children != null ? isCitationOnlyNode(el.props.children) : true;
    }
  }
  return false;
}

function extractText(children: ReactNode): string {
  if (typeof children === "string") return children;
  if (typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(extractText).join("");
  if (children && typeof children === "object" && "props" in (children as object)) {
    const el = children as React.ReactElement<{ children?: ReactNode }>;
    return extractText(el.props?.children);
  }
  return "";
}

function WikiImage({ rawUrl, alt }: { rawUrl: string; alt: string }) {
  const [expanded, setExpanded] = useState(false);
  const [failed, setFailed] = useState(false);
  const proxyPath = proxyPathFor(rawUrl);

  if (failed) {
    return (
      <span className="inline-flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-sm text-muted-foreground my-2">
        <ImageIcon className="h-4 w-4 text-blue-400" />
        {alt}
      </span>
    );
  }

  // Issue #89 — proxied (Slack file) images go through ProxiedImage for
  // signed-token resolution; public images render directly.
  const thumbnail = proxyPath ? (
    <ProxiedImage
      unproxiedUrl={rawUrl}
      mediaPath={proxyPath}
      alt={alt}
      className="rounded-lg border border-border max-h-80 object-contain bg-muted/20"
      onError={() => setFailed(true)}
    />
  ) : (
    <img
      src={rawUrl}
      alt={alt}
      className="rounded-lg border border-border max-h-80 object-contain bg-muted/20"
      onError={() => setFailed(true)}
    />
  );

  const expandedView = proxyPath ? (
    <ProxiedImage
      unproxiedUrl={rawUrl}
      mediaPath={proxyPath}
      alt={alt}
      className="max-w-full max-h-[70vh] object-contain rounded-lg shadow-2xl"
    />
  ) : (
    <img src={rawUrl} alt={alt} className="max-w-full max-h-[70vh] object-contain rounded-lg shadow-2xl" />
  );

  return (
    <>
      <span className="block my-4 group cursor-pointer" onClick={() => setExpanded(true)}>
        {thumbnail}
        <span className="flex items-center gap-1.5 mt-1.5 text-xs text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity">
          <Maximize2 className="h-3 w-3" /> Click to enlarge
        </span>
      </span>
      {expanded && (
        <div className="fixed inset-0 z-[100] bg-background/80 backdrop-blur-sm flex items-center justify-center p-4 sm:p-12" onClick={() => setExpanded(false)}>
          <div className="relative max-w-4xl w-full flex flex-col items-center" onClick={e => e.stopPropagation()}>
            <button onClick={() => setExpanded(false)} className="absolute -top-10 right-0 p-1.5 rounded-md text-muted-foreground hover:text-foreground transition-colors">
              <X className="h-4 w-4" />
            </button>
            {expandedView}
            {alt && alt !== "Image" && (
              <span className="mt-3 text-xs text-muted-foreground text-center max-w-lg">{alt}</span>
            )}
          </div>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Name-description bullet → styled table promoter.
//
// LLM-generated wiki bodies frequently emit lists of the shape:
//
//   - **Repository name** — Beever Atlas Documentation Repository
//   - **Public Visibility** — Both repos are public for community access
//   - **Mattermost Permissions** — Full administrative access required
//
// Today these render as plain bullets, which buries the structure.
// When ALL items in a ``<ul>`` start with a ``<strong>`` and a
// separator (em-dash, en-dash, hyphen, or colon), we promote the
// list to a 2-column table so the visual reads as the data shape it
// actually is. The existing bullet path is preserved for mixed lists.
//
// Threshold of 3 is conservative — short ad-hoc lists keep their
// bullet shape; only lists that look like reference data become
// tables.
// ---------------------------------------------------------------------------

const NAME_DESC_PROMOTE_THRESHOLD = 3;
// Em-dash, en-dash, double-hyphen, single-hyphen-with-spaces, or colon.
// All four are observed separators in the LLM output.
const NAME_DESC_SEPARATOR_RE = /^\s*(?:[—–]|--|-(?=\s)|:)\s+([\s\S]*)$/;

function asReactElement(
  node: ReactNode,
): React.ReactElement<{ children?: ReactNode }> | null {
  if (node && typeof node === "object" && "props" in (node as object)) {
    return node as React.ReactElement<{ children?: ReactNode }>;
  }
  return null;
}

function tryExtractNameDescRow(
  liNode: ReactNode,
): { name: ReactNode; desc: ReactNode[] } | null {
  const li = asReactElement(liNode);
  if (!li) return null;
  const liChildren = li.props?.children;
  if (liChildren == null) return null;

  // ReactMarkdown wraps li content in a <p> for loose lists, and the
  // existing ``processChildren`` (which the ``li`` component override
  // calls before mounting) wraps multi-node content in a Fragment.
  // Unwrap iteratively (capped to avoid infinite loops on pathological
  // shapes) so the bold-prefix detection below sees the actual
  // leading <strong> element.
  let arr: ReactNode[] = Array.isArray(liChildren) ? liChildren : [liChildren];
  for (let depth = 0; depth < 4; depth++) {
    if (arr.length !== 1) break;
    const sole = asReactElement(arr[0]);
    if (!sole) break;
    const wrapperType = (sole as unknown as { type: unknown }).type;
    if (
      wrapperType === "p" ||
      wrapperType === "div" ||
      wrapperType === Fragment
    ) {
      const inner = sole.props?.children;
      arr = Array.isArray(inner) ? inner : [inner];
      continue;
    }
    break;
  }

  // First non-whitespace child must be a <strong> element. Whitespace-only
  // strings are skipped so leading indentation in the source doesn't
  // disqualify a row.
  let firstIdx = -1;
  for (let i = 0; i < arr.length; i++) {
    const c = arr[i];
    if (typeof c === "string" && c.trim() === "") continue;
    firstIdx = i;
    break;
  }
  if (firstIdx < 0) return null;
  const first = asReactElement(arr[firstIdx]);
  if (!first) return null;
  // The leading element must render as bold. Two shapes are accepted:
  //   - the raw HTML tag ``<strong>`` (no components-dict override)
  //   - the strong-override function whose ``name`` / ``displayName``
  //     is ``"strong"`` (the case in this renderer — see the
  //     ``components.strong`` entry below)
  const firstType = (first as unknown as { type: unknown }).type;
  const firstFn = firstType as { name?: string; displayName?: string };
  const isStrong =
    firstType === "strong" ||
    (typeof firstType === "function" &&
      (firstFn.name === "strong" || firstFn.displayName === "strong"));
  if (!isStrong) return null;

  const name = first.props?.children;
  const rest = arr.slice(firstIdx + 1);

  // Concatenate the leading text of the rest to test for the separator.
  // We only need the FIRST string-typed node — the separator always
  // appears at the start of the body text (em-dash / colon).
  const restText = rest.map(extractText).join("");
  const m = restText.match(NAME_DESC_SEPARATOR_RE);
  if (!m) return null;

  // Strip the separator from the leading text node so the desc cell
  // starts cleanly. Subsequent nodes (citations, wikilinks, etc.) are
  // preserved by reference so their interactivity survives.
  const stripped: ReactNode[] = [];
  let separatorRemoved = false;
  for (const node of rest) {
    if (!separatorRemoved && typeof node === "string") {
      const replaced = node.replace(NAME_DESC_SEPARATOR_RE, "$1");
      if (replaced !== node) {
        separatorRemoved = true;
        if (replaced.length > 0) stripped.push(replaced);
        continue;
      }
    }
    stripped.push(node);
  }
  return { name, desc: stripped };
}

interface NameDescTableProps {
  rows: { name: ReactNode; desc: ReactNode[] }[];
}

function NameDescTable({ rows }: NameDescTableProps) {
  return (
    <div className="overflow-x-auto my-4">
      <table className="min-w-full text-sm border-collapse border border-border rounded-md">
        <thead>
          <tr>
            <th className="border border-border bg-muted px-3 py-2 text-left font-semibold text-foreground w-1/3 min-w-[12ch]">
              Name
            </th>
            <th className="border border-border bg-muted px-3 py-2 text-left font-semibold text-foreground">
              Description
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className={i % 2 === 1 ? "bg-muted/20" : undefined}>
              <td className="border border-border px-3 py-2 align-top text-foreground font-medium">
                {row.name}
              </td>
              <td className="border border-border px-3 py-2 align-top text-muted-foreground">
                {row.desc}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function WikiPdfLink({ href, title }: { href: string; title: string }) {
  const [expanded, setExpanded] = useState(false);
  const proxied = proxyUrl(href);

  return (
    <>
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-3 rounded-lg border border-border bg-card hover:bg-muted/50 px-4 py-3 my-2 w-full text-left transition-colors group"
      >
        <FileText className="h-5 w-5 text-red-400 shrink-0" />
        <div className="flex-1 min-w-0">
          <span className="text-sm font-medium text-foreground group-hover:text-primary truncate block">{title}</span>
          <span className="text-xs text-muted-foreground">PDF Document — click to {expanded ? "collapse" : "preview"}</span>
        </div>
        <ExternalLink className="h-3.5 w-3.5 text-muted-foreground shrink-0" onClick={(e) => { e.stopPropagation(); window.open(proxied, "_blank"); }} />
      </button>
      {expanded && (
        <div className="my-2 rounded-lg border border-border overflow-hidden">
          <iframe src={proxied} className="w-full h-[500px] bg-white" title={title} />
        </div>
      )}
    </>
  );
}

export function WikiMarkdown({
  content,
  citations = [],
  onNavigate,
  crossLinks = {},
  crossLinksBroken = [],
}: WikiMarkdownProps) {
  const processed = preprocessContent(content);
  // Bundle the marker-resolution state for `processChildren` so we can
  // pass it through every component-renderer closure without recreating
  // it per call.
  const ci: MarkerContext = {
    citations,
    crossLinks,
    crossLinksBroken: new Set(crossLinksBroken),
  };

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        code({ className, children, ...props }) {
          const lang = className?.replace("language-", "") ?? "";
          const code = String(children).replace(/\n$/, "");

          if (lang === "mermaid") return <MermaidBlock chart={code} />;
          if (lang === "chart" || lang.startsWith("chart")) return <ChartBlock spec={code} />;

          // LLM sometimes emits chart JSON inside ```json blocks — detect and render as chart
          if ((lang === "json" || lang === "") && code.trimStart().startsWith("{")) {
            try {
              const parsed = JSON.parse(code);
              const t = parsed?.type;
              if (t === "bar" || t === "area" || t === "donut" || t === "pie") {
                return <ChartBlock spec={code} />;
              }
            } catch { /* not valid JSON or not a chart */ }
          }

          return (
            <code className={`${className ?? ""} rounded bg-muted px-1 py-0.5 text-sm font-mono text-foreground`} {...props}>
              {children}
            </code>
          );
        },
        pre({ children }) {
          return <pre className="rounded-lg bg-muted p-4 overflow-x-auto text-sm">{children}</pre>;
        },
        p({ children }) {
          const processed = processChildren(children, ci);
          // Detect citation-only paragraphs (e.g. standalone [1] after media)
          // Check if all non-whitespace content is citation markers
          const raw = extractText(children as ReactNode).replace(/\s+/g, " ").trim();
          const withoutCites = raw.replace(/\u200Bcite:\d+\u200B/g, "").trim();
          // Also check the processed children — if they only contain CitationLink components
          const isCitationOnly = (raw.length > 0 && withoutCites === "") || isCitationOnlyNode(processed);
          if (isCitationOnly) {
            return <p className="flex justify-end -mt-5 mb-1 mr-1 relative z-10">{processed}</p>;
          }
          return <p className="text-foreground/90 leading-relaxed my-2">{processed}</p>;
        },
        li({ children }) {
          return <li className="text-sm leading-relaxed">{processChildren(children, ci, "li")}</li>;
        },
        td({ children }) {
          return <td className="border border-border px-3 py-2 text-muted-foreground">{processChildren(children, ci, "td")}</td>;
        },
        th({ children }) {
          return <th className="border border-border bg-muted px-3 py-2 text-left font-semibold text-foreground">{processChildren(children, ci, "th")}</th>;
        },
        strong({ children }) {
          return <strong className="font-semibold text-foreground">{processChildren(children, ci, "s")}</strong>;
        },
        em({ children }) {
          return <em>{processChildren(children, ci, "em")}</em>;
        },
        blockquote({ children }) {
          const text = extractText(children as ReactNode).trim();
          const calloutMatch = text.match(/\[!(NOTE|TIP|WARNING)\]\s*([\s\S]*)/);
          if (calloutMatch) {
            return <CalloutBox type={calloutMatch[1].toLowerCase() as "note" | "tip" | "warning"} content={calloutMatch[2].trim()} />;
          }
          return <blockquote className="border-l-4 border-border pl-4 italic text-muted-foreground">{children}</blockquote>;
        },
        h1({ children }) {
          return <h1 className="text-2xl font-bold text-foreground mt-6 mb-3">{processChildren(children, ci, "h1")}</h1>;
        },
        h2({ children }) {
          const text = extractText(children as ReactNode);
          const id = text.toLowerCase().replace(/[^a-z0-9\s-]/g, "").replace(/\s+/g, "-").slice(0, 80);
          return <h2 id={id} className="text-xl font-semibold text-foreground mt-5 mb-2 scroll-mt-6">{processChildren(children, ci, "h2")}</h2>;
        },
        h3({ children }) {
          const text = extractText(children as ReactNode);
          const id = text.toLowerCase().replace(/[^a-z0-9\s-]/g, "").replace(/\s+/g, "-").slice(0, 80);
          return <h3 id={id} className="text-lg font-semibold text-foreground/90 mt-4 mb-2 scroll-mt-6">{processChildren(children, ci, "h3")}</h3>;
        },
        h4({ children }) {
          return <h4 className="text-base font-semibold text-foreground/90 mt-3 mb-1">{processChildren(children, ci, "h4")}</h4>;
        },
        ul({ children }) {
          // Detect lists where every item is a ``**Name** — desc`` pair
          // and ≥3 items match. Promote those to a styled 2-column
          // table so the visual reads as the structured data it is,
          // not as a sea of bullets. Mixed lists or short lists fall
          // through to the existing bullet renderer.
          const liNodes = Array.isArray(children) ? children : [children];
          const candidateRows: { name: ReactNode; desc: ReactNode[] }[] = [];
          let allMatch = true;
          for (const node of liNodes) {
            if (node == null || node === false || node === true) continue;
            if (typeof node === "string" && node.trim() === "") continue;
            const row = tryExtractNameDescRow(node);
            if (!row) {
              allMatch = false;
              break;
            }
            candidateRows.push(row);
          }
          if (allMatch && candidateRows.length >= NAME_DESC_PROMOTE_THRESHOLD) {
            return <NameDescTable rows={candidateRows} />;
          }
          return <ul className="list-disc list-inside space-y-1 my-3 text-foreground/80">{children}</ul>;
        },
        ol({ children }) {
          return <ol className="list-decimal list-inside space-y-1 my-3 text-foreground/80">{children}</ol>;
        },
        img({ src, alt }) {
          if (!src) return <span className="text-muted-foreground text-sm italic">[Image: {alt}]</span>;
          // Pass the raw src; WikiImage decides whether to proxy via
          // ProxiedImage (Slack files) or render directly.
          return <WikiImage rawUrl={src} alt={alt || "Image"} />;
        },
        a({ href, children }) {
          if (!href) return <span>{children}</span>;
          const text = String(children);

          // Internal wiki link — `[Title](/wiki/<slug>)`. The LLM
          // emits these in See Also / Related / children TOC sections
          // and the legacy renderer treats them as external (opens
          // in a new tab → 404 since the route is SPA-handled).
          // Intercept and route through `onNavigate` with the slug
          // so the click stays in-app and lands on the right page.
          if (href.startsWith("/wiki/")) {
            const slug = href.slice("/wiki/".length).split(/[/?#]/)[0];
            return (
              <a
                href={href}
                onClick={(e) => {
                  e.preventDefault();
                  if (slug && onNavigate) onNavigate(slug);
                }}
                className="text-primary hover:underline font-medium"
              >
                {children}
              </a>
            );
          }

          const mediaType = detectMediaType(href, text);

          // Image-typed links — when the LLM emits ``[Title](image_url)``
          // instead of ``![alt](image_url)`` (a routine mistake on
          // chat-platform attachments), render as an actual image
          // preview via WikiImage. The proxied/public URL routing
          // inside WikiImage handles Mattermost/Slack file URLs that
          // need the loader-token signing path.
          if (mediaType === "image") {
            return <WikiImage rawUrl={href} alt={text || "Image"} />;
          }

          // Video-typed links — render as an inline embed so the user
          // can play without leaving the wiki. YouTube + Vimeo
          // transform to embed iframes; direct .mp4/.webm get a
          // native <video> element.
          if (mediaType === "video") {
            const lower = href.toLowerCase();
            const ytMatch = lower.match(/[?&]v=([\w-]+)/) || lower.match(/youtu\.be\/([\w-]+)/);
            const vimeoMatch = lower.match(/vimeo\.com\/(\d+)/);
            if (ytMatch) {
              return (
                <div className="relative w-full my-3 rounded-lg overflow-hidden border border-border bg-muted/20" style={{ paddingBottom: "56.25%" }}>
                  <iframe
                    src={`https://www.youtube.com/embed/${ytMatch[1]}`}
                    title={text || "Embedded video"}
                    loading="lazy"
                    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                    allowFullScreen
                    className="absolute inset-0 w-full h-full border-0"
                  />
                </div>
              );
            }
            if (vimeoMatch) {
              return (
                <div className="relative w-full my-3 rounded-lg overflow-hidden border border-border bg-muted/20" style={{ paddingBottom: "56.25%" }}>
                  <iframe
                    src={`https://player.vimeo.com/video/${vimeoMatch[1]}`}
                    title={text || "Embedded video"}
                    loading="lazy"
                    allow="autoplay; fullscreen; picture-in-picture"
                    allowFullScreen
                    className="absolute inset-0 w-full h-full border-0"
                  />
                </div>
              );
            }
            // Direct .mp4/.webm — native video element
            if (/\.(mp4|webm)(\?|$)/.test(lower)) {
              return (
                <video
                  src={href}
                  controls
                  preload="metadata"
                  aria-label={text || "Video"}
                  className="w-full h-auto my-3 rounded-lg border border-border bg-muted/20"
                />
              );
            }
            // Fall through to generic link card if we can't embed.
          }

          // PDF links — show expandable card
          if (mediaType === "pdf" || text.startsWith("📄")) {
            const pdfTitle = text.replace(/^📄\s*/, "").trim();
            return <WikiPdfLink href={href} title={pdfTitle || "PDF Document"} />;
          }

          // All other links — card style matching PDF cards
          const cleanText = text.replace(/[\p{Emoji_Presentation}\p{Extended_Pictographic}\u200D\uFE0F]/gu, "").trim();
          const domain = (() => { try { return new URL(href).hostname.replace("www.", ""); } catch { return href; } })();
          const icon = mediaType === "video"
            ? <Film className="h-5 w-5 text-purple-400 shrink-0" />
            : <Link2 className="h-5 w-5 text-blue-400 shrink-0" />;
          const title = cleanText || domain;
          return (
            <a href={href} target="_blank" rel="noopener noreferrer"
              className="flex items-center gap-3 rounded-lg border border-border bg-card hover:bg-muted/50 px-4 py-3 my-2 w-full text-left no-underline transition-colors group">
              {icon}
              <div className="flex-1 min-w-0">
                <span className="text-sm font-medium text-foreground group-hover:text-primary truncate block">{title}</span>
                <span className="text-xs text-muted-foreground">{domain}</span>
              </div>
              <ExternalLink className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
            </a>
          );
        },
        table({ children }) {
          return <div className="overflow-x-auto my-4"><table className="min-w-full text-sm border-collapse border border-border">{children}</table></div>;
        },
        hr() {
          return <hr className="border-border my-6" />;
        },
      }}
    >
      {processed}
    </ReactMarkdown>
  );
}
