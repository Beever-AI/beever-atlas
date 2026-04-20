import { useState, useCallback, useRef, useEffect } from "react";
import type { Message, MessageCitations, AskMetadata, ToolCallEvent, AnswerMode, AttachmentFile, DecompositionPlan } from "../types/askTypes";
import { authFetch } from "../lib/api";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

interface UseAskReturn {
  ask: (question: string, options?: { mode?: AnswerMode; attachments?: AttachmentFile[] }) => Promise<void>;
  retry: () => void;
  abort: () => void;
  reset: () => void;
  loadSession: (messages: Message[], sessionId: string) => void;
  messages: Message[];
  isStreaming: boolean;
  error: string | null;
  sessionId: string | null;
  // Backwards-compat: derived from tail assistant message
  response: string;
  thinking: string[];
  citations: MessageCitations;
  metadata: AskMetadata | null;
  toolCalls: ToolCallEvent[];
}

// ---------------------------------------------------------------------------
// Typewriter helper
// ---------------------------------------------------------------------------
// Returns a controller that drips buffered text into React state at ~25ms per
// tick. Call `feed(text)` to add chars; `flush()` to drain immediately;
// `cancel()` on unmount/abort.

interface TypewriterController {
  feed: (text: string) => void;
  flush: () => void;
  cancel: () => void;
}

function createTypewriter(
  apply: (chars: string) => void,
  intervalMs = 20,
): TypewriterController {
  let pending = "";
  let timer: ReturnType<typeof setInterval> | null = null;
  const CHARS_PER_TICK = 4; // release up to 4 chars every 20ms (~200 chars/s)

  const start = () => {
    if (timer !== null) return;
    timer = setInterval(() => {
      if (!pending) {
        clearInterval(timer!);
        timer = null;
        return;
      }
      const chunk = pending.slice(0, CHARS_PER_TICK);
      pending = pending.slice(CHARS_PER_TICK);
      apply(chunk);
    }, intervalMs);
  };

  return {
    feed(text: string) {
      pending += text;
      start();
    },
    flush() {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
      if (pending) {
        apply(pending);
        pending = "";
      }
    },
    cancel() {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
      pending = "";
    },
  };
}

export function useAsk(channelId: string): UseAskReturn {
  const [messages, setMessages] = useState<Message[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sessionIdRef = useRef<string>(crypto.randomUUID());
  const idleTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Typewriter controllers — reset per ask call
  const contentTwRef = useRef<TypewriterController | null>(null);
  const thinkingTwRef = useRef<TypewriterController | null>(null);

  // Cancel typewriters on unmount
  useEffect(() => {
    return () => {
      contentTwRef.current?.cancel();
      thinkingTwRef.current?.cancel();
    };
  }, []);

  const clearIdleTimeout = useCallback(() => {
    if (idleTimeoutRef.current) {
      clearTimeout(idleTimeoutRef.current);
      idleTimeoutRef.current = null;
    }
  }, []);

  const abort = useCallback(() => {
    clearIdleTimeout();
    abortRef.current?.abort();
  }, [clearIdleTimeout]);

  const reset = useCallback(() => {
    clearIdleTimeout();
    abortRef.current?.abort();
    setMessages([]);
    setError(null);
    setSessionId(null);
    sessionIdRef.current = crypto.randomUUID();
  }, [clearIdleTimeout]);

  const ask = useCallback(
    async (question: string, options?: { mode?: AnswerMode; attachments?: AttachmentFile[]; disabled_tools?: string[] }) => {
      if (abortRef.current) {
        abortRef.current.abort();
      }
      // Cancel any in-flight typewriters from a previous ask
      contentTwRef.current?.cancel();
      thinkingTwRef.current?.cancel();

      const controller = new AbortController();
      abortRef.current = controller;

      setError(null);

      const userMsgId = crypto.randomUUID();
      const assistantMsgId = crypto.randomUUID();

      setMessages((prev) => [
        ...prev,
        {
          id: userMsgId,
          role: "user",
          content: question,
          citations: [],
          toolCalls: [],
          thinking: [],
          metadata: null,
          isStreaming: false,
          attachments: options?.attachments,
        },
        {
          id: assistantMsgId,
          role: "assistant",
          content: "",
          citations: [],
          toolCalls: [],
          thinking: [],
          metadata: null,
          isStreaming: true,
        },
      ]);

      // Update the specific assistant message by id (safe across concurrent asks)
      const updateAssistant = (updater: (msg: Message) => Message) => {
        setMessages((prev) =>
          prev.map((msg) => (msg.id === assistantMsgId ? updater(msg) : msg)),
        );
      };

      // Set up typewriter controllers for this ask
      contentTwRef.current = createTypewriter((chars) => {
        updateAssistant((msg) => ({ ...msg, content: msg.content + chars }));
      });
      thinkingTwRef.current = createTypewriter((chars) => {
        updateAssistant((msg) => {
          const prev = msg.thinking;
          // Append chars to the last thinking segment, or start a new one
          if (prev.length === 0) return { ...msg, thinking: [chars] };
          return { ...msg, thinking: [...prev.slice(0, -1), prev[prev.length - 1] + chars] };
        });
      });

      try {
        const res = await authFetch(`${API_BASE}/api/channels/${channelId}/ask`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question,
            session_id: sessionIdRef.current,
            mode: options?.mode ?? "deep",
            attachments: options?.attachments ?? [],
            disabled_tools: options?.disabled_tools ?? [],
          }),
          signal: controller.signal,
        });

        if (!res.ok) {
          throw new Error(`Server returned ${res.status}`);
        }

        const reader = res.body?.getReader();
        if (!reader) throw new Error("No response body");

        const decoder = new TextDecoder();
        let buffer = "";
        let currentEventType = "";

        // Idle timeout: reset on each received chunk. If no data arrives
        // for 45 seconds, auto-recover the UI. This protects against
        // network-level stream truncation the backend cannot detect.
        const resetIdleTimeout = () => {
          clearIdleTimeout();
          idleTimeoutRef.current = setTimeout(() => {
            updateAssistant((msg) => ({ ...msg, isStreaming: false }));
            controller.abort();
          }, 45_000);
        };

        resetIdleTimeout();

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          resetIdleTimeout();

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            if (line.startsWith("event: ")) {
              currentEventType = line.slice(7);
            } else if (line.startsWith("data: ") && currentEventType) {
              try {
                const data = JSON.parse(line.slice(6));
                switch (currentEventType) {
                  case "thinking":
                    thinkingTwRef.current?.feed(data.text || "");
                    break;
                  case "response_delta":
                    contentTwRef.current?.feed(data.delta || "");
                    break;
                  case "decomposition":
                    updateAssistant((msg) => ({
                      ...msg,
                      decomposition: data as DecompositionPlan,
                    }));
                    break;
                  case "citations":
                    updateAssistant((msg) => ({
                      ...msg,
                      // Phase 2: preserve structured envelope when present;
                      // fall back to the legacy items list for flag-off path.
                      citations: Array.isArray(data.sources)
                        ? {
                            items: data.items || [],
                            sources: data.sources,
                            refs: data.refs || [],
                          }
                        : data.items || [],
                    }));
                    break;
                  case "metadata":
                    updateAssistant((msg) => ({ ...msg, metadata: data }));
                    if (data.session_id) {
                      setSessionId(data.session_id);
                    }
                    break;
                  case "tool_call_start":
                    updateAssistant((msg) => ({
                      ...msg,
                      toolCalls: [
                        ...msg.toolCalls,
                        {
                          tool_name: data.tool_name,
                          input: data.input || {},
                          status: "running" as const,
                          started_at: Date.now(),
                        },
                      ],
                    }));
                    break;
                  case "tool_call_end":
                    updateAssistant((msg) => {
                      const tcs = [...(msg.toolCalls ?? [])];
                      const idx = tcs.findIndex(
                        (tc) => tc.tool_name === data.tool_name && tc.status === "running"
                      );
                      if (idx >= 0) {
                        tcs[idx] = {
                          ...tcs[idx],
                          status: "done" as const,
                          result_summary: data.result_summary,
                          latency_ms: data.latency_ms,
                          facts_found: data.facts_found,
                        };
                      }
                      return { ...msg, toolCalls: tcs };
                    });
                    break;
                  case "follow_ups":
                    updateAssistant((msg) => ({
                      ...msg,
                      followUps: data.suggestions ?? [],
                    }));
                    break;
                  case "thinking_done":
                    updateAssistant((msg) => ({
                      ...msg,
                      thinkingDuration: data.duration_ms ?? null,
                    }));
                    break;
                  case "error":
                    setError(data.message || "Unknown error");
                    updateAssistant((msg) => ({ ...msg, isStreaming: false }));
                    break;
                  case "done":
                    // Delay flush so the typewriter interval can drain several
                    // ticks first (~6 ticks × 20ms = 120ms), then force-flush
                    // anything still buffered. Other done-side-effects run
                    // immediately and are not blocked by this timeout.
                    setTimeout(() => {
                      contentTwRef.current?.flush();
                      thinkingTwRef.current?.flush();
                    }, 150);
                    updateAssistant((msg) => {
                      const cleanedContent = msg.content
                        .replace(/\n*---\n*FOLLOW_UPS:\s*\[.*?\]/s, "")
                        .trimEnd();
                      return { ...msg, content: cleanedContent, isStreaming: false };
                    });
                    break;
                }
              } catch {
                // Skip unparseable lines
              }
              currentEventType = "";
            }
          }
        }

        // Flush remaining decoder bytes and buffer after stream closes
        const trailing = decoder.decode() + buffer;
        if (trailing.trim()) {
          const trailingLines = trailing.split("\n");
          for (const line of trailingLines) {
            if (line.startsWith("event: ")) {
              currentEventType = line.slice(7);
            } else if (line.startsWith("data: ") && currentEventType) {
              try {
                const data = JSON.parse(line.slice(6));
                if (currentEventType === "done") {
                  updateAssistant((msg) => ({ ...msg, isStreaming: false }));
                } else if (currentEventType === "error") {
                  setError(data.message || "Unknown error");
                  updateAssistant((msg) => ({ ...msg, isStreaming: false }));
                }
              } catch {
                // Skip unparseable trailing data
              }
              currentEventType = "";
            }
          }
        }

        // Safety net: always reset streaming after the reader loop exits,
        // regardless of whether a "done" event was received. This is
        // idempotent — if done already set isStreaming: false, this is a no-op.
        contentTwRef.current?.flush();
        thinkingTwRef.current?.flush();
        updateAssistant((msg) => ({ ...msg, isStreaming: false }));
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") {
          // Keep partial content, just stop streaming; cancel pending typewriter chars
          contentTwRef.current?.cancel();
          thinkingTwRef.current?.cancel();
          updateAssistant((msg) => ({ ...msg, isStreaming: false }));
          return;
        }
        contentTwRef.current?.cancel();
        thinkingTwRef.current?.cancel();
        setError(err instanceof Error ? err.message : "Unknown error");
        updateAssistant((msg) => ({ ...msg, isStreaming: false }));
      } finally {
        clearIdleTimeout();
        abortRef.current = null;
      }
    },
    [channelId],
  );

  const loadSession = useCallback((loadedMessages: Message[], loadedSessionId: string) => {
    setMessages(loadedMessages);
    setSessionId(loadedSessionId);
    sessionIdRef.current = loadedSessionId;
    setError(null);
  }, []);

  const retry = useCallback(() => {
    const lastUserMsg = [...messages].reverse().find(m => m.role === "user");
    if (lastUserMsg) {
      setMessages(prev => {
        // Remove the last assistant message (failed)
        const withoutLastAssistant =
          prev.length > 0 && prev[prev.length - 1].role === "assistant"
            ? prev.slice(0, prev.length - 1)
            : prev;
        // Also remove the user message that preceded it (will be re-added by ask)
        if (
          withoutLastAssistant.length > 0 &&
          withoutLastAssistant[withoutLastAssistant.length - 1].role === "user"
        ) {
          return withoutLastAssistant.slice(0, -1);
        }
        return withoutLastAssistant;
      });
      setError(null);
      ask(lastUserMsg.content);
    }
  }, [messages, ask]);

  // Backwards-compat: derived from the tail assistant message
  const tailMsg =
    messages.length > 0 && messages[messages.length - 1].role === "assistant"
      ? messages[messages.length - 1]
      : null;

  return {
    ask,
    retry,
    abort,
    reset,
    loadSession,
    messages,
    isStreaming: tailMsg?.isStreaming ?? false,
    error,
    sessionId,
    response: tailMsg?.content ?? "",
    thinking: tailMsg?.thinking ?? [],
    citations: tailMsg?.citations ?? [],
    metadata: tailMsg?.metadata ?? null,
    toolCalls: tailMsg?.toolCalls ?? [],
  };
}
