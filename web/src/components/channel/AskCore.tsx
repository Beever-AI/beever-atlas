import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import { useAsk } from "@/hooks/useAsk";
import { useAskSession } from "@/hooks/useAskSession";
import { useAskSessions } from "@/contexts/AskSessionsContext";
import { useFeedback } from "@/hooks/useFeedback";
import { useFileUpload } from "@/hooks/useFileUpload";
import { ChatMessageList } from "./ChatMessageList";
import { ChatInputBar } from "./ChatInputBar";
import { ChannelPicker } from "@/components/ask/ChannelPicker";
import { Share2, Check } from "lucide-react";
import type { AnswerMode, Message } from "@/types/askTypes";
import type { ChannelOption } from "@/components/ask/ChannelPicker";

interface AskCoreProps {
  /**
   * When "fixed": channel is bound to `channelId` prop for every turn (legacy
   * channel-page AskTab behavior). Session is channel-scoped via useAsk.
   * When "picker": channel is a per-turn selection from the inline picker.
   * Session is global via useAskSession.
   */
  channelMode: "fixed" | "picker";
  /** Fixed mode: channel for all turns. Picker mode: initial default. */
  channelId: string;
  initialQuery?: string;
  /** Picker mode only: available channels for the inline picker. */
  availableChannels?: ChannelOption[];
  /** Picker mode: the sessionId from the URL path (`/ask/:sessionId`). */
  urlSessionId?: string;
  /** Picker mode: called once when a new session id is minted from SSE metadata. */
  onSessionMinted?: (sessionId: string) => void;
}

export function AskCore({
  channelMode,
  channelId,
  initialQuery,
  availableChannels = [],
  urlSessionId,
  onSessionMinted,
}: AskCoreProps) {
  if (channelMode === "fixed") {
    return (
      <AskCoreFixed
        channelId={channelId}
        initialQuery={initialQuery}
      />
    );
  }
  return (
    <AskCorePicker
      initialChannelId={channelId}
      initialQuery={initialQuery}
      availableChannels={availableChannels}
      urlSessionId={urlSessionId}
      onSessionMinted={onSessionMinted}
    />
  );
}

// ---------------------------------------------------------------------------
// Fixed mode — channel-scoped, legacy behavior for channel-page AskTab
// ---------------------------------------------------------------------------

function AskCoreFixed({
  channelId,
  initialQuery,
}: {
  channelId: string;
  initialQuery?: string;
}) {
  const {
    ask,
    abort,
    reset,
    retry,
    loadSession,
    messages,
    isStreaming,
    error,
    sessionId,
  } = useAsk(channelId);

  const sessions = useAskSessions();
  const feedback = useFeedback(channelId);
  const fileUpload = useFileUpload(channelId);

  const [mode, setMode] = useState<AnswerMode>("deep");

  useEffect(() => {
    if (sessionId) sessions.setActiveSessionId(sessionId);
  }, [sessionId, sessions.setActiveSessionId]);

  useEffect(() => {
    if (!isStreaming) sessions.fetchSessions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isStreaming]);

  useEffect(() => {
    reset();
  }, [channelId, reset]);

  useEffect(() => {
    if (sessions.activeSessionId && sessions.activeSessionId !== sessionId) {
      sessions.loadSession(sessions.activeSessionId).then((msgs) => {
        if (msgs.length > 0) loadSession(msgs, sessions.activeSessionId!);
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions.activeSessionId]);

  useEffect(() => {
    if (sessions.activeSessionId === null && sessionId !== null) reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions.activeSessionId]);

  const handleSubmit = useCallback(
    (question: string, options?: { mode?: AnswerMode }) => {
      ask(question, {
        mode: options?.mode ?? mode,
        attachments: fileUpload.files,
      });
      fileUpload.clearFiles();
    },
    [ask, mode, fileUpload],
  );

  const handleFollowUp = useCallback((q: string) => handleSubmit(q), [handleSubmit]);

  const handleFeedback = useCallback(
    (messageId: string, rating: "up" | "down", comment?: string) => {
      if (sessionId) feedback.submitFeedback(sessionId, messageId, rating, comment);
    },
    [sessionId, feedback],
  );

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.shiftKey && e.key === "o") {
        e.preventDefault();
        sessions.newConversation();
        reset();
      }
      if (mod && e.shiftKey && e.key === "c") {
        const last = [...messages].reverse().find((m) => m.role === "assistant");
        if (last?.content) {
          e.preventDefault();
          navigator.clipboard.writeText(last.content);
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [messages, sessions, reset]);

  return (
    <div className="flex flex-col h-full bg-background">
      {error && (
        <div className="flex items-center gap-2 px-4 py-2">
          <div className="ml-auto flex items-center gap-2">
            <span className="text-xs text-red-400">{error}</span>
            <button
              onClick={retry}
              className="text-xs px-2 py-1 bg-red-500/20 text-red-400 rounded hover:bg-red-500/30 transition-colors"
            >
              Retry
            </button>
          </div>
        </div>
      )}

      <ChatMessageList
        messages={messages}
        isLoading={
          isStreaming &&
          messages.length > 0 &&
          messages[messages.length - 1]?.role === "user"
        }
        onFollowUpClick={handleFollowUp}
        onFeedback={handleFeedback}
        feedbackMap={feedback.feedbackMap}
        sessionId={sessionId ?? undefined}
      />

      <ChatInputBar
        onSubmit={handleSubmit}
        onAbort={abort}
        isStreaming={isStreaming}
        mode={mode}
        onModeChange={setMode}
        attachments={fileUpload.files}
        onFileUpload={(file) => fileUpload.uploadFile(file)}
        onRemoveAttachment={fileUpload.removeFile}
        uploading={fileUpload.uploading}
        initialValue={initialQuery}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Picker mode — v2 flow, channel is per-turn, inline picker in input bar
// ---------------------------------------------------------------------------

function AskCorePicker({
  initialChannelId,
  initialQuery,
  availableChannels,
  urlSessionId,
  onSessionMinted,
}: {
  initialChannelId: string;
  initialQuery?: string;
  availableChannels: ChannelOption[];
  urlSessionId?: string;
  onSessionMinted?: (sessionId: string) => void;
}) {
  const {
    ask,
    abort,
    reset,
    retry,
    loadSession,
    messages,
    isStreaming,
    error,
    sessionId,
    disabledTools,
    toggleTool,
    toolDescriptors,
    phase,
  } = useAskSession();

  const sessions = useAskSessions();

  // Use an empty string channelId for file upload since uploads are channel-less in v2
  const fileUpload = useFileUpload("");
  const feedback = useFeedback("");

  const [mode, setMode] = useState<AnswerMode>("deep");
  const [activeChannelId, setActiveChannelId] = useState<string>(initialChannelId);
  const [shareCopied, setShareCopied] = useState(false);
  const handleShare = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setShareCopied(true);
      setTimeout(() => setShareCopied(false), 1500);
    } catch {
      /* ignore */
    }
  }, []);

  // Build channelId → name lookup for badges
  const channelNames = useMemo(() => {
    const map: Record<string, string> = {};
    for (const c of availableChannels) map[c.channel_id] = c.name;
    return map;
  }, [availableChannels]);

  // If the initial channel (from URL ?context= or empty) isn't in the
  // available list, fall back to the first available channel. This only
  // runs when the channel list changes — it MUST NOT react to
  // `activeChannelId` changes, otherwise it could overwrite a user-driven
  // selection (e.g. a channel resolved from loading a legacy session whose
  // channel the user has since left).
  useEffect(() => {
    setActiveChannelId((current) => {
      if (availableChannels.length === 0) return current;
      if (current && availableChannels.find((c) => c.channel_id === current)) {
        return current;
      }
      return availableChannels[0].channel_id;
    });
  }, [availableChannels]);

  // Guard concurrent session loads: if the user clicks multiple sessions
  // rapidly, only the latest resolve should apply to the UI.
  const loadSeqRef = useRef(0);

  // Sync session id to context so sidebar can highlight active session
  useEffect(() => {
    if (sessionId) sessions.setActiveSessionId(sessionId);
  }, [sessionId, sessions.setActiveSessionId]);

  // Fire navigate(replace) exactly once on creating → streaming for a session
  // minted on bare /ask. `mintedRef` guards against the effect running twice
  // (React strict-mode double-invoke) for the same id.
  const mintedRef = useRef<string | null>(null);
  useEffect(() => {
    if (phase !== "streaming") return;
    if (!sessionId) return;
    if (mintedRef.current === sessionId) return;
    // Skip if URL already matches — this is a follow-up turn on an existing
    // session, not a cold mint.
    if (urlSessionId === sessionId) return;
    mintedRef.current = sessionId;
    onSessionMinted?.(sessionId);
  }, [phase, sessionId, urlSessionId, onSessionMinted]);

  // Refresh session list after streaming completes
  useEffect(() => {
    if (!isStreaming) sessions.fetchSessions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isStreaming]);

  // `initialQuery` (e.g. /ask?q=...) is handed to the input bar as a draft;
  // we deliberately do not auto-submit — the user decides when to send.

  // Respond to sidebar-driven session selection. Uses a monotonic sequence
  // ref so rapid clicks don't let an older resolve overwrite a newer one.
  useEffect(() => {
    if (sessions.activeSessionId && sessions.activeSessionId !== sessionId) {
      const targetSessionId = sessions.activeSessionId;
      const seq = ++loadSeqRef.current;
      sessions.loadSession(targetSessionId).then((msgs) => {
        // Stale resolve — a newer load has superseded this one
        if (seq !== loadSeqRef.current) return;
        if (msgs.length === 0) return;
        loadSession(msgs as Message[], targetSessionId);
        // Prefer the most recent turn's channel that is still available.
        // Falls back to the first available channel; never overwrites with
        // an unavailable id (user may have left that channel).
        const lastWithChannel = [...msgs].reverse().find((m) => m.channel_id);
        const candidate = lastWithChannel?.channel_id;
        if (candidate && availableChannels.find((c) => c.channel_id === candidate)) {
          setActiveChannelId(candidate);
        }
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions.activeSessionId]);

  // Respond to "+ New chat" from sidebar
  useEffect(() => {
    if (sessions.activeSessionId === null && sessionId !== null) reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions.activeSessionId]);

  const handleSubmit = useCallback(
    (question: string, options?: { mode?: AnswerMode }) => {
      if (!activeChannelId) return;
      ask(question, {
        channelId: activeChannelId,
        mode: options?.mode ?? mode,
        attachments: fileUpload.files,
      });
      fileUpload.clearFiles();
    },
    [ask, activeChannelId, mode, fileUpload],
  );

  const handleFollowUp = useCallback((q: string) => handleSubmit(q), [handleSubmit]);

  const handleFeedback = useCallback(
    (messageId: string, rating: "up" | "down", comment?: string) => {
      if (sessionId) feedback.submitFeedback(sessionId, messageId, rating, comment);
    },
    [sessionId, feedback],
  );

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.shiftKey && e.key === "o") {
        e.preventDefault();
        sessions.newConversation();
        reset();
      }
      if (mod && e.shiftKey && e.key === "c") {
        const last = [...messages].reverse().find((m) => m.role === "assistant");
        if (last?.content) {
          e.preventDefault();
          navigator.clipboard.writeText(last.content);
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [messages, sessions, reset]);

  const channelPicker = (
    <ChannelPicker
      channels={availableChannels}
      value={activeChannelId}
      onChange={setActiveChannelId}
      disabled={isStreaming}
    />
  );

  return (
    <div className="flex flex-col h-full bg-background">
      {sessionId && messages.length > 0 && (
        <div className="flex items-center justify-end gap-2 px-4 py-1 border-b border-border/40">
          <button
            onClick={handleShare}
            className="inline-flex items-center gap-1 text-xs h-7 px-2 rounded-md border border-border bg-card hover:bg-muted text-foreground"
            data-testid="ask-share-button"
            aria-label="Share conversation"
          >
            {shareCopied ? <Check size={12} /> : <Share2 size={12} />}
            {shareCopied ? "Copied" : "Share"}
          </button>
        </div>
      )}

      {error && (
        <div className="flex items-center gap-2 px-4 py-2">
          <div className="ml-auto flex items-center gap-2">
            <span className="text-xs text-red-400">{error}</span>
            <button
              onClick={retry}
              className="text-xs px-2 py-1 bg-red-500/20 text-red-400 rounded hover:bg-red-500/30 transition-colors"
            >
              Retry
            </button>
          </div>
        </div>
      )}

      <ChatMessageList
        messages={messages}
        isLoading={
          isStreaming &&
          messages.length > 0 &&
          messages[messages.length - 1]?.role === "user"
        }
        onFollowUpClick={handleFollowUp}
        onFeedback={handleFeedback}
        feedbackMap={feedback.feedbackMap}
        sessionId={sessionId ?? undefined}
        channelNames={channelNames}
        activeChannelId={activeChannelId}
      />

      <ChatInputBar
        onSubmit={handleSubmit}
        onAbort={abort}
        isStreaming={isStreaming}
        mode={mode}
        onModeChange={setMode}
        attachments={fileUpload.files}
        onFileUpload={(file) => fileUpload.uploadFile(file)}
        onRemoveAttachment={fileUpload.removeFile}
        uploading={fileUpload.uploading}
        channelPicker={channelPicker}
        disabled={!activeChannelId}
        initialValue={initialQuery}
        placeholder={
          activeChannelId
            ? `Ask Beever about #${channelNames[activeChannelId] ?? "this channel"}…`
            : "Choose a channel to start asking…"
        }
        toolDescriptors={toolDescriptors.length > 0 ? toolDescriptors : undefined}
        disabledTools={disabledTools}
        onToggleTool={toggleTool}
      />
    </div>
  );
}
