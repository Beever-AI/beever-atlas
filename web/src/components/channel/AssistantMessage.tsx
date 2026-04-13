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

  const wantsInline =
    ref?.inline === true &&
    !!source &&
    source.attachments.length > 0 &&
    ctx.inlineUsed.size < MAX_INLINE_MEDIA &&
    !ctx.inlineUsed.has(n);

  if (wantsInline && source) {
    ctx.inlineUsed.add(n);
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
  };

  // Memoize the markdown tree so React doesn't re-parse the same prefix on
  // every typewriter tick. Re-renders only when body content actually changes.
  const markdownTree = useMemo(
    () =>
      body ? (
        <div className="prose prose-invert prose-sm max-w-none text-foreground/90">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              p: ({ children }) => (
                <p className="mb-3 leading-relaxed">
                  {renderWithCitationChips(children, ctx)}
                </p>
              ),
              li: ({ children }) => (
                <li className="text-foreground/90">
                  {renderWithCitationChips(children, ctx)}
                </li>
              ),
              ul: ({ children }) => <ul className="mb-3 space-y-1 list-disc list-inside">{children}</ul>,
              ol: ({ children }) => <ol className="mb-3 space-y-1 list-decimal list-inside">{children}</ol>,
              code: ({ className, children, ...props }) => {
                if (className === "language-mermaid") {
                  return <MermaidBlock code={String(children).replace(/\n$/, "")} />;
                }
                const isInline = !className;
                return isInline ? (
                  <code className="px-1.5 py-0.5 bg-muted rounded text-primary text-xs" {...props}>{children}</code>
                ) : (
                  <code className={`block p-3 bg-muted rounded-lg text-xs overflow-x-auto ${className ?? ""}`} {...props}>{children}</code>
                );
              },
              table: ({ children }) => (
                <div className="overflow-x-auto mb-3">
                  <table className="text-sm border-collapse border border-border">{children}</table>
                </div>
              ),
              th: ({ children }) => <th className="px-3 py-2 bg-muted border border-border text-left text-foreground/90">{children}</th>,
              td: ({ children }) => <td className="px-3 py-2 border border-border text-muted-foreground">{children}</td>,
              blockquote: ({ children }) => (
                <blockquote className="border-l-2 border-border pl-4 text-muted-foreground italic mb-3">{children}</blockquote>
              ),
              a: ({ href, children }) => (
                <a href={href} target="_blank" rel="noopener noreferrer" className="text-primary hover:text-primary/80 underline">{children}</a>
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
