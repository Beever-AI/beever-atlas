import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { type ReactNode, useState } from "react";
import { FileText, ExternalLink, Link2, Image as ImageIcon, Film, X, Maximize2 } from "lucide-react";
import { MermaidBlock } from "./MermaidBlock";
import { ChartBlock } from "./ChartBlock";
import { CalloutBox } from "./CalloutBox";
import { CitationLink } from "./CitationLink";
import { buildLoaderUrl } from "@/lib/api";
import { ProxiedImage } from "@/components/common/ProxiedImage";
import type { WikiCitation } from "@/lib/types";

/** Returns the route path needed to mint a signed loader token, or null
 * if the URL is public (no proxy needed).
 *
 * Compares the parsed-URL hostname against the allowlist instead of
 * `url.includes("files.slack.com")` — substring matching against the full
 * URL would treat `evil.com/files.slack.com` as a Slack file and 404 on
 * the proxy with attacker-controlled bytes in the URL. CodeQL alert #20.
 */
function proxyPathFor(url: string): string | null {
  let host: string;
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    return null;
  }
  if (host === "files.slack.com") {
    return `/api/files/proxy?url=${encodeURIComponent(url)}`;
  }
  return null;
}

/** Synchronous fallback (raw key) for `<a href>` / `<iframe src>` cases
 * that cannot await an async mint. Issue #89 migration follow-up tracked
 * separately. */
function proxyUrl(url: string): string {
  const p = proxyPathFor(url);
  return p ? buildLoaderUrl(p) : url;
}

function detectMediaType(url: string, alt?: string): "image" | "pdf" | "video" | "link" {
  const lower = (url + (alt || "")).toLowerCase();
  if (lower.match(/\.(png|jpg|jpeg|gif|webp|svg)(\?|$)/)) return "image";
  if (lower.match(/\.(pdf)(\?|$)/) || lower.includes("pdf")) return "pdf";
  if (lower.match(/\.(mp4|mov|webm|avi)(\?|$)/) || lower.includes("video")) return "video";
  return "link";
}

interface WikiMarkdownProps {
  content: string;
  citations?: WikiCitation[];
  onNavigate?: (pageId: string) => void;
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

/**
 * Process text to render citation markers as interactive CitationLink components.
 */
function processText(
  text: string,
  citations: WikiCitation[],
  keyPrefix: string,
): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /\u200Bcite:(\d+)\u200B/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const idx = parseInt(match[1], 10);
    parts.push(
      <CitationLink key={`${keyPrefix}-${match.index}`} index={idx} citation={citations[idx - 1]} />,
    );
    lastIndex = pattern.lastIndex;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return parts;
}

/**
 * Walk React children tree and process all text nodes for citations.
 */
function processChildren(
  children: ReactNode,
  citations: WikiCitation[],
  keyPrefix = "c",
): ReactNode {
  if (typeof children === "string") {
    const parts = processText(children, citations, keyPrefix);
    return parts.length === 1 && typeof parts[0] === "string" ? parts[0] : <>{parts}</>;
  }
  if (Array.isArray(children)) {
    return <>{children.map((child, i) => processChildren(child, citations, `${keyPrefix}-${i}`))}</>;
  }
  if (children && typeof children === "object" && "props" in (children as object)) {
    const el = children as React.ReactElement<{ children?: ReactNode }>;
    if (el.props?.children != null) {
      const processed = processChildren(el.props.children, citations, `${keyPrefix}-el`);
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

export function WikiMarkdown({ content, citations = [], onNavigate: _onNavigate }: WikiMarkdownProps) {
  const processed = preprocessContent(content);
  const ci = citations; // capture for closures

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
          const mediaType = detectMediaType(href, text);

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
