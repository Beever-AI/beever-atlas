import { useRef, useEffect, useState } from "react";
import type { Message } from "@/types/askTypes";
import { UserMessage } from "./UserMessage";
import { AssistantMessage } from "./AssistantMessage";
import { MessageSkeleton } from "./MessageSkeleton";
import { ChevronDown } from "lucide-react";

interface ChatMessageListProps {
  messages: Message[];
  isLoading: boolean;
  onCitationClick?: (citation: any) => void;
  onFollowUpClick?: (question: string) => void;
  onFeedback?: (messageId: string, rating: "up" | "down", comment?: string) => void;
  feedbackMap?: Record<string, { rating: "up" | "down"; comment?: string }>;
  sessionId?: string;
  /** Map of channel_id → display name for per-message channel badges (v2 flow). */
  channelNames?: Record<string, string>;
  /** Currently selected channel — shown in the empty-state hero (v2 flow). */
  activeChannelId?: string;
}

const DEFAULT_SUGGESTIONS = [
  "What is this channel about?",
  "Who are the key contributors?",
  "What decisions were made recently?",
  "What topics are discussed most?",
];

export function ChatMessageList({
  messages,
  isLoading,
  onCitationClick,
  onFollowUpClick,
  onFeedback,
  feedbackMap = {},
  sessionId,
  channelNames,
  activeChannelId,
}: ChatMessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const [userScrolled, setUserScrolled] = useState(false);

  useEffect(() => {
    if (!userScrolled) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, userScrolled]);

  const handleScroll = () => {
    if (!containerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
    const atBottom = scrollHeight - scrollTop - clientHeight < 100;
    setShowScrollBtn(!atBottom);
    setUserScrolled(!atBottom);
  };

  const scrollToBottom = () => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    setUserScrolled(false);
  };

  // Empty state — vertically balanced hero + card grid. Sits in the chat
  // area above the composer; offset slightly upward for optical balance.
  if (messages.length === 0 && !isLoading) {
    const channelName = activeChannelId ? channelNames?.[activeChannelId] : null;

    return (
      <div className="flex-1 overflow-y-auto">
        <div className="min-h-full flex flex-col items-center justify-center px-6 sm:px-8 pb-[10vh] pt-10 motion-safe:animate-rise-in">
          <section className="flex flex-col items-center gap-4 text-center max-w-xl">
            <h1 className="font-heading text-[32px] tracking-tight text-foreground">
              {channelName ? (
                <>
                  Ask about{" "}
                  <span className="text-primary">#{channelName}</span>
                </>
              ) : (
                "What would you like to know?"
              )}
            </h1>
            <p className="text-muted-foreground text-base">
              {channelName
                ? `Anything in #${channelName}'s knowledge is fair game.`
                : "Choose a channel in the composer below to start."}
            </p>
          </section>

          <section className="grid grid-cols-1 sm:grid-cols-2 gap-4 max-w-3xl w-full mt-10">
            {DEFAULT_SUGGESTIONS.map((q, i) => (
              <button
                key={q}
                onClick={() => onFollowUpClick?.(q)}
                className="group relative bg-card rounded-2xl border border-border p-5 text-left hover:bg-muted/30 hover:border-primary/40 hover:shadow-sm transition-all duration-200 motion-safe:animate-rise-in"
                style={{ animationDelay: `${i * 55}ms` }}
              >
                <p className="text-sm text-foreground leading-relaxed pr-6">
                  {q}
                </p>
                <span
                  aria-hidden
                  className="absolute right-4 top-1/2 -translate-y-1/2 text-muted-foreground/30 group-hover:text-primary group-hover:translate-x-0.5 transition-all"
                >
                  →
                </span>
              </button>
            ))}
          </section>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 relative overflow-hidden">
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="h-full overflow-y-auto px-4 md:px-8 py-6"
      >
        <div className="max-w-3xl mx-auto w-full space-y-6">
          {messages.map((msg) =>
            msg.role === "user" ? (
              <UserMessage
                key={msg.id}
                message={msg}
                channelNames={channelNames}
              />
            ) : (
              <AssistantMessage
                key={msg.id}
                message={msg}
                onCitationClick={onCitationClick}
                onFollowUpClick={onFollowUpClick}
                onFeedback={onFeedback}
                feedback={feedbackMap[msg.id]}
                sessionId={sessionId}
              />
            ),
          )}
          {isLoading && messages[messages.length - 1]?.role === "user" && <MessageSkeleton />}
          <div ref={bottomRef} />
        </div>
      </div>

      {showScrollBtn && (
        <button
          onClick={scrollToBottom}
          className="absolute bottom-4 left-1/2 -translate-x-1/2 bg-card hover:bg-muted text-foreground border border-border rounded-full p-2 shadow-lg transition-all"
          title="Scroll to latest"
        >
          <ChevronDown className="w-4 h-4" />
        </button>
      )}
    </div>
  );
}
