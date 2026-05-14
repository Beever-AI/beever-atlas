import { useRef, useEffect, useState } from "react";
import type { Message } from "@/types/askTypes";
import { UserMessage } from "./UserMessage";
import { AssistantMessage } from "./AssistantMessage";
import { MessageSkeleton } from "./MessageSkeleton";
import {
  ArrowRight,
  BookOpen,
  ChevronDown,
  Download,
  Loader2,
  MessageCircleQuestion,
  Sparkles,
} from "lucide-react";
import type { WikiState } from "@/hooks/useWikiStates";

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
  /** Wiki readiness for the currently selected channel — drives the empty-state
   *  header variant (ready / building / no-wiki). Defaults to "ready" so callers
   *  that don't have the data render the current hero unchanged. */
  activeChannelWikiState?: WikiState;
  /** Optional callback for the "Ingest this channel" CTA shown when the active
   *  channel has no wiki yet. When omitted, the CTA falls back to a static
   *  hint pointing the user at the channel page. */
  onIngestChannel?: () => void;
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
  activeChannelWikiState = "ready",
  onIngestChannel,
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
    const isReady = activeChannelWikiState === "ready" || !channelName;
    const isBuilding = activeChannelWikiState === "building";
    const isEmpty = activeChannelWikiState === "empty" || activeChannelWikiState === "errored";

    // Header content varies with wiki state so the user knows up-front
    // whether to expect real answers, partial answers, or to ingest first.
    let hero: React.ReactNode;
    if (!channelName) {
      hero = (
        <>
          <h1 className="font-heading text-[32px] tracking-tight text-foreground flex items-center gap-3">
            <BookOpen className="w-7 h-7 text-primary/80" />
            What would you like to know?
          </h1>
          <p className="text-muted-foreground text-base">
            Choose a channel in the composer below to start.
          </p>
        </>
      );
    } else if (isBuilding) {
      hero = (
        <>
          <h1 className="font-heading text-[32px] tracking-tight text-foreground flex items-center gap-3">
            <Loader2 className="w-7 h-7 text-amber-500 motion-safe:animate-spin" />
            Asking <span className="text-primary">#{channelName}</span>
          </h1>
          <p className="text-muted-foreground text-base">
            Building <span className="text-foreground/80">#{channelName}</span>'s wiki —
            answers may be incomplete while ingestion is in progress.
          </p>
        </>
      );
    } else if (isEmpty) {
      hero = (
        <div className="relative flex flex-col items-center gap-6 text-center max-w-xl mx-auto">
          {/* Soft radial halo behind the icon — sets a quiet focal point
              without competing with the CTA. */}
          <div
            aria-hidden
            className="absolute -top-10 left-1/2 -translate-x-1/2 w-72 h-72 rounded-full bg-primary/[0.06] blur-3xl pointer-events-none"
          />

          {/* Icon medallion — a book waiting to be opened. The small
              "+" badge signals "ready to be built". */}
          <div className="relative flex items-center justify-center w-20 h-20 rounded-2xl border border-border bg-card/70 backdrop-blur-sm shadow-sm">
            <BookOpen
              className="w-9 h-9 text-muted-foreground/40"
              strokeWidth={1.5}
            />
            <span className="absolute -bottom-1.5 -right-1.5 w-6 h-6 rounded-full bg-primary text-primary-foreground flex items-center justify-center text-sm font-semibold border-2 border-background shadow-md">
              +
            </span>
          </div>

          {/* Status label + channel name — two-line hierarchy beats the
              old inline icon+title which looked cramped. */}
          <div className="flex flex-col items-center gap-2">
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground/60">
              No wiki yet
            </span>
            <h1 className="font-heading text-[36px] tracking-tight text-foreground leading-none">
              <span className="text-primary/40 mr-0.5">#</span>
              {channelName}
            </h1>
          </div>

          {/* Friendlier description — leads with the action ("ingest")
              rather than the absence ("hasn't learned anything"). */}
          <p className="text-muted-foreground text-[15px] leading-relaxed max-w-sm">
            Ingest{" "}
            <span className="text-foreground/80 font-medium">
              #{channelName}
            </span>{" "}
            and Beever Atlas will read its messages and build a wiki you can ask anything about.
          </p>

          {/* CTA + time hint */}
          {onIngestChannel && (
            <div className="flex flex-col items-center gap-2.5 mt-1">
              <button
                type="button"
                onClick={onIngestChannel}
                className="group inline-flex items-center gap-2.5 pl-5 pr-6 py-3 rounded-full bg-primary text-primary-foreground text-sm font-medium shadow-lg shadow-primary/25 hover:shadow-xl hover:shadow-primary/35 hover:brightness-110 active:scale-[0.98] transition-all duration-200"
              >
                <Download className="w-4 h-4" strokeWidth={2.25} />
                <span>Ingest this channel</span>
                <ArrowRight
                  className="w-4 h-4 -ml-1 opacity-0 -translate-x-1 group-hover:opacity-100 group-hover:translate-x-0 transition-all"
                  strokeWidth={2.25}
                />
              </button>
              <span className="text-[11px] text-muted-foreground/50">
                Usually takes 1–2 minutes
              </span>
            </div>
          )}

          {/* What you'll get — three quiet benefit pills */}
          <div className="flex items-center gap-3 mt-3 text-[11px] text-muted-foreground/55">
            <span className="inline-flex items-center gap-1.5">
              <BookOpen className="w-3.5 h-3.5" strokeWidth={1.75} />
              Browsable wiki
            </span>
            <span aria-hidden className="w-1 h-1 rounded-full bg-muted-foreground/25" />
            <span className="inline-flex items-center gap-1.5">
              <MessageCircleQuestion className="w-3.5 h-3.5" strokeWidth={1.75} />
              Ask anything
            </span>
            <span aria-hidden className="w-1 h-1 rounded-full bg-muted-foreground/25" />
            <span className="inline-flex items-center gap-1.5">
              <Sparkles className="w-3.5 h-3.5" strokeWidth={1.75} />
              Auto-updates
            </span>
          </div>
        </div>
      );
    } else {
      // isReady
      hero = (
        <>
          <h1 className="font-heading text-[32px] tracking-tight text-foreground flex items-center gap-3">
            <BookOpen className="w-7 h-7 text-primary" />
            Ask about <span className="text-primary">#{channelName}</span>
          </h1>
          <p className="text-muted-foreground text-base">
            Anything in #{channelName}'s knowledge is fair game.
          </p>
        </>
      );
    }

    // Suggested questions only make sense when there's actually a wiki to
    // pull answers from — hide them on the empty-state to avoid setting up
    // a question that returns "no knowledge".
    const showSuggestions = isReady || isBuilding;

    return (
      <div className="flex-1 overflow-y-auto">
        <div className="min-h-full flex flex-col items-center justify-center px-6 sm:px-8 pb-[10vh] pt-10 motion-safe:animate-rise-in">
          <section className="flex flex-col items-center gap-4 text-center max-w-xl">
            {hero}
          </section>

          {showSuggestions && (
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
          )}
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
        <div className="max-w-3xl mx-auto w-full flex flex-col gap-8">
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
