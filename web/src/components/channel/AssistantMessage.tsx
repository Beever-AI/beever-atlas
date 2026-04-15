import { Children, Fragment, isValidElement, memo, useMemo, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  CitationRef,
  Message,
  Source,
} from "@/types/askTypes";
import { Reasoning } from "./Reasoning";
import { ToolList } from "./ToolList";
import { QueryPlan } from "./QueryPlan";
import { Sources } from "./Sources";
import { CitationChip } from "./CitationChip";
import { InlineMedia } from "./InlineMedia";
import { MarkdownImage } from "./MarkdownImage";
import { AnswerActions } from "./AnswerActions";
import { FollowUpSuggestions } from "./FollowUpSuggestions";
import { selectCitations, stripSourcesBlock } from "@/lib/citations";
import { MermaidBlock } from "./MermaidBlock";

interface AssistantMessageProps {
  message: Message;
  onCitationClick?: (citation: any) => void;
  onFollowUpClick?: (question: string) => void;
  onFeedback?: (messageId: string, rating: "up" | "down", comment?: string) => void;
  feedback?: { rating: "up" | "down"; comment?: string };
  sessionId?: string;
}

// Cap on inline media substitutions per answer to keep visual density reasonable.
const MAX_INLINE_MEDIA = 4;

interface CitationContext {
  messageId: string;
  sources: Source[];
  refs: CitationRef[];
  /** Markers the rewriter has already resolved to inline media (to cap count). */
  inlineUsed: Set<number>;
  /** Whether we've already auto-inlined one file (PDF/document) attachment. */
  fileInlineUsed: boolean;
}

/** Split a text node on inline `[N]` markers, substituting chips or media. */
function renderWithCitationChips(
  children: ReactNode,
  ctx: CitationContext,
): ReactNode {
  const maxIndex = ctx.refs.reduce((m, r) => Math.max(m, r.marker), 0);
  if (maxIndex <= 0) return children;

  const arr = Children.toArray(children);
  return arr.map((child, i) => {
    if (typeof child === "string") {
      return splitOnMarkers(child, ctx, maxIndex, `c${i}`);
    }
    return isValidElement(child) ? child : child;
  });
}

function splitOnMarkers(
  text: string,
  ctx: CitationContext,
  maxIndex: number,
  keyPrefix: string,
): ReactNode {
  const re = /\[(\d+)\]/g;
  const parts: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = re.exec(text)) !== null) {
    const n = Number(match[1]);
    // Emit leading text (excluding trailing whitespace when we're about
    // to strip an orphan marker, so we don't leave a double space).
    const leading = text.slice(last, match.index);
    if (n >= 1 && n <= maxIndex) {
      if (leading) parts.push(leading);
      parts.push(renderMarker(n, ctx, `${keyPrefix}-${i}`));
    } else {
      // Orphan marker (LLM invented a bare `[N]` without a corresponding
      // `[src:xxx]` tag). Strip silently rather than render a broken chip
      // or confusing literal; log once per message for observability.
      logOrphanMarker(ctx.messageId, n, maxIndex);
      // Keep the text-before but collapse a trailing space if present —
      // avoids a lingering double space after the strip.
      parts.push(leading.replace(/[\t ]+$/, ""));
    }
    last = re.lastIndex;
    i += 1;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts.length ? <Fragment>{parts}</Fragment> : text;
}

// Per-message seen set keeps orphan-marker warnings quiet.
const _orphanLoggedFor = new Set<string>();

function logOrphanMarker(messageId: string, marker: number, maxIndex: number) {
  const key = `${messageId}:${marker}`;
  if (_orphanLoggedFor.has(key)) return;
  _orphanLoggedFor.add(key);
  if (import.meta.env?.DEV) {
    // eslint-disable-next-line no-console
    console.debug(
      `[citations] stripped orphan marker [${marker}] in message ${messageId} (maxIndex=${maxIndex})`,
    );
  }
}

function renderMarker(
  n: number,
  ctx: CitationContext,
  key: string,
): ReactNode {
  const ref = ctx.refs.find((r) => r.marker === n);
  const source = ref ? ctx.sources.find((s) => s.id === ref.source_id) : undefined;

  // Auto-upgrade to inline when the source carries visual media or a file,
  // even if the LLM forgot the `inline` tag. Images/videos always upgrade.
  // PDFs and documents upgrade at most once per answer so link cards don't
  // flood every citation. Plain `link_preview` still requires the explicit
  // `inline` tag.
  const firstAttachment = source?.attachments[0];
  const isVisualMedia =
    firstAttachment?.kind === "image" ||
    firstAttachment?.kind === "video";
  const isFileAttachment =
    firstAttachment?.kind === "pdf" ||
    firstAttachment?.kind === "document";
  const fileAlreadyShown = ctx.fileInlineUsed;
  const canUpgradeFile = isFileAttachment && !fileAlreadyShown;
  const wantsInline =
    !!source &&
    !!firstAttachment &&
    (ref?.inline === true || isVisualMedia || canUpgradeFile) &&
    ctx.inlineUsed.size < MAX_INLINE_MEDIA &&
    !ctx.inlineUsed.has(n);

  if (wantsInline && source) {
    ctx.inlineUsed.add(n);
    if (canUpgradeFile) {
      ctx.fileInlineUsed = true;
    }
    return (
      <InlineMedia
        key={key}
        attachment={source.attachments[0]}
        source={source}
        n={n}
        messageId={ctx.messageId}
      />
    );
  }

  return (
    <CitationChip key={key} n={n} messageId={ctx.messageId} source={source} />
  );
}

function AssistantMessageInner({
  message,
  onFollowUpClick,
  onFeedback,
  feedback,
}: AssistantMessageProps) {
  const { body, strippedCitations } = stripSourcesBlock(message.content ?? "");
  const { sources, refs } = selectCitations(message.citations, strippedCitations);

  const ctx: CitationContext = {
    messageId: message.id,
    sources,
    refs,
    inlineUsed: new Set<number>(),
    fileInlineUsed: false,
  };

  // Memoize the markdown tree so React doesn't re-parse the same prefix on
  // every typewriter tick. Re-renders only when body content actually changes.
  const markdownTree = useMemo(
    () =>
      body ? (
        <div className="max-w-none text-[15px] leading-relaxed text-foreground/90">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              h1: ({ children }) => (
                <h1 className="text-2xl font-bold text-foreground mt-6 mb-3 first:mt-0 tracking-tight">
                  {children}
                </h1>
              ),
              h2: ({ children }) => (
                <h2 className="text-xl font-bold text-foreground mt-6 mb-3 first:mt-0 tracking-tight">
                  {children}
                </h2>
              ),
              h3: ({ children }) => (
                <h3 className="text-base font-semibold text-foreground mt-5 mb-2 first:mt-0">
                  {children}
                </h3>
              ),
              h4: ({ children }) => (
                <h4 className="text-sm font-semibold text-foreground mt-4 mb-2 first:mt-0 uppercase tracking-wide text-foreground/80">
                  {children}
                </h4>
              ),
              p: ({ children }) => (
                <p className="mb-3 leading-relaxed text-[15px]">
                  {renderWithCitationChips(children, ctx)}
                </p>
              ),
              strong: ({ children }) => (
                <strong className="font-semibold text-foreground">{children}</strong>
              ),
              em: ({ children }) => (
                <em className="italic text-foreground/90">{children}</em>
              ),
              hr: () => <hr className="border-border my-6" />,
              li: ({ children }) => (
                <li className="text-foreground/90 leading-relaxed marker:text-muted-foreground/70">
                  {renderWithCitationChips(children, ctx)}
                </li>
              ),
              ul: ({ children }) => (
                <ul className="mb-4 ml-5 space-y-1.5 list-disc">{children}</ul>
              ),
              ol: ({ children }) => (
                <ol className="mb-4 ml-5 space-y-1.5 list-decimal">{children}</ol>
              ),
              code: ({ className, children, ...props }) => {
                if (className === "language-mermaid") {
                  return <MermaidBlock code={String(children).replace(/\n$/, "")} />;
                }
                const isInline = !className;
                return isInline ? (
                  <code className="px-1.5 py-0.5 bg-muted rounded text-primary text-[13px] font-mono" {...props}>{children}</code>
                ) : (
                  <code className={`block p-3 bg-muted rounded-lg text-[13px] font-mono overflow-x-auto my-3 ${className ?? ""}`} {...props}>{children}</code>
                );
              },
              pre: ({ children }) => <pre className="mb-3">{children}</pre>,
              table: ({ children }) => (
                <div className="overflow-x-auto mb-4 rounded-lg border border-border">
                  <table className="w-full text-sm border-collapse">{children}</table>
                </div>
              ),
              thead: ({ children }) => <thead className="bg-muted/60">{children}</thead>,
              tbody: ({ children }) => <tbody className="divide-y divide-border">{children}</tbody>,
              tr: ({ children }) => <tr className="hover:bg-muted/30 transition-colors">{children}</tr>,
              th: ({ children }) => (
                <th className="px-3 py-2 text-left font-semibold text-foreground border-b border-border">
                  {children}
                </th>
              ),
              td: ({ children }) => (
                <td className="px-3 py-2 text-foreground/90 align-top">
                  {renderWithCitationChips(children, ctx)}
                </td>
              ),
              blockquote: ({ children }) => (
                <blockquote className="border-l-2 border-primary/40 pl-4 text-muted-foreground italic my-3">
                  {children}
                </blockquote>
              ),
              a: ({ href, children }) => (
                <a href={href} target="_blank" rel="noopener noreferrer" className="text-primary hover:text-primary/80 underline underline-offset-2">{children}</a>
              ),
              img: ({ src, alt }) => (
                <MarkdownImage
                  src={typeof src === "string" ? src : undefined}
                  alt={typeof alt === "string" ? alt : undefined}
                />
              ),
            }}
          >
            {body}
          </ReactMarkdown>
        </div>
      ) : null,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [body, refs],
  );

  return (
    <div className="min-w-0 max-w-none">
      {/* Query decomposition plan */}
      {message.decomposition && (
        <QueryPlan plan={message.decomposition} isStreaming={message.isStreaming} />
      )}

      {/* Thinking */}
      {message.thinking && message.thinking.length > 0 && (
        <Reasoning
          thinking={message.thinking}
          isStreaming={message.isStreaming}
          durationMs={message.thinkingDuration ?? null}
        />
      )}

      {/* Tool calls */}
      {message.toolCalls && message.toolCalls.length > 0 && (
        <ToolList
          toolCalls={message.toolCalls}
          isStreaming={message.isStreaming}
        />
      )}

      {/* Response content */}
      {markdownTree}

      {/* Streaming indicator */}
      {message.isStreaming && !message.content && message.thinking?.length === 0 && (
        <div className="flex items-center gap-1.5 text-muted-foreground text-sm">
          <span className="flex gap-1">
            <span className="w-1.5 h-1.5 bg-muted-foreground/60 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
            <span className="w-1.5 h-1.5 bg-muted-foreground/60 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
            <span className="w-1.5 h-1.5 bg-muted-foreground/60 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
          </span>
        </div>
      )}

      {/* Sources */}
      {!message.isStreaming && sources.length > 0 && (
        <Sources sources={sources} refs={refs} messageId={message.id} />
      )}

      {/*
        Muted footer shown when the backend stripped orphan [N] markers or
        unknown `src:` tags from the stream. TODO: once the SSE citations
        event carries `meta.warnings` (stream_rewriter.get_stats), wire it
        through Message.citations.meta. Until then this gates on an
        optional field so the existing payload contract is unchanged.
      */}
      {!message.isStreaming &&
        ((message.citations as any)?.meta?.warnings ?? 0) > 0 && (
          <p
            className="mt-2 text-xs text-muted-foreground/70"
            role="note"
            aria-label="Some references were unavailable"
          >
            Some references were unavailable.
          </p>
        )}

      {/* Actions */}
      {!message.isStreaming && body && (
        <AnswerActions
          message={message}
          onFeedback={onFeedback}
          feedback={feedback}
        />
      )}

      {/* Follow-up suggestions */}
      {!message.isStreaming && message.followUps && message.followUps.length > 0 && (
        <FollowUpSuggestions
          suggestions={message.followUps}
          onSelect={onFollowUpClick}
        />
      )}
    </div>
  );
}

export const AssistantMessage = memo(AssistantMessageInner);
