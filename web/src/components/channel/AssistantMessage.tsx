import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Message } from "@/types/askTypes";
import { Reasoning } from "./Reasoning";
import { ToolList } from "./ToolList";
import { AnswerActions } from "./AnswerActions";
import { FollowUpSuggestions } from "./FollowUpSuggestions";

interface AssistantMessageProps {
  message: Message;
  onCitationClick?: (citation: any) => void;
  onFollowUpClick?: (question: string) => void;
  onFeedback?: (messageId: string, rating: "up" | "down", comment?: string) => void;
  feedback?: { rating: "up" | "down"; comment?: string };
  sessionId?: string;
}

export function AssistantMessage({
  message,
  onCitationClick,
  onFollowUpClick,
  onFeedback,
  feedback,
  sessionId,
}: AssistantMessageProps) {
  return (
    <div className="min-w-0 max-w-none">
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
      {message.content && (
        <div className="prose prose-invert prose-sm max-w-none text-foreground/90">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              p: ({ children }) => <p className="mb-3 leading-relaxed">{children}</p>,
              ul: ({ children }) => <ul className="mb-3 space-y-1 list-disc list-inside">{children}</ul>,
              ol: ({ children }) => <ol className="mb-3 space-y-1 list-decimal list-inside">{children}</ol>,
              li: ({ children }) => <li className="text-foreground/90">{children}</li>,
              code: ({ className, children, ...props }) => {
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
            {message.content}
          </ReactMarkdown>
        </div>
      )}

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

      {/* Actions */}
      {!message.isStreaming && message.content && (
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
