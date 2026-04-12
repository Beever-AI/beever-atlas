import { useState, useCallback, useRef } from "react";
import type { Message, Citation, AskMetadata, ToolCallEvent, AnswerMode, AttachmentFile } from "../types/askTypes";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

interface AskCallOptions {
  channelId: string;
  mode?: AnswerMode;
  attachments?: AttachmentFile[];
}

interface UseAskSessionReturn {
  ask: (question: string, options: AskCallOptions) => Promise<void>;
  retry: () => void;
  abort: () => void;
  reset: () => void;
  loadSession: (messages: Message[], sessionId: string) => void;
  messages: Message[];
  isStreaming: boolean;
  error: string | null;
  sessionId: string | null;
  response: string;
  thinking: string[];
  citations: Citation[];
  metadata: AskMetadata | null;
  toolCalls: ToolCallEvent[];
}

/**
 * Session-scoped ask hook: channelId is a per-call argument, not a hook arg.
 * Posts to POST /api/ask (v2 endpoint). Each message carries its own channel_id.
 *
 * Mirrors the SSE streaming flow of useAsk(channelId) but without session-level
 * channel binding, so a single conversation can span multiple channels.
 */
export function useAskSession(): UseAskSessionReturn {
  const [messages, setMessages] = useState<Message[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Lazy — only allocate a session id when the user actually submits. This
  // prevents phantom sessions during React StrictMode re-mounts or route churn.
  const sessionIdRef = useRef<string | null>(null);
  const idleTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastCallRef = useRef<{ question: string; options: AskCallOptions } | null>(null);

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
    // Lazy: next ask() will allocate a fresh session id
    sessionIdRef.current = null;
  }, [clearIdleTimeout]);

  const ask = useCallback(
    async (question: string, options: AskCallOptions) => {
      if (!options.channelId) {
        setError("A channel must be selected before asking.");
        return;
      }

      lastCallRef.current = { question, options };

      // Lazy: allocate a session id on first actual submit
      if (sessionIdRef.current === null) {
        sessionIdRef.current = crypto.randomUUID();
      }

      if (abortRef.current) {
        abortRef.current.abort();
      }

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
          channel_id: options.channelId,
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
          channel_id: options.channelId,
        },
      ]);

      const updateAssistant = (updater: (msg: Message) => Message) => {
        setMessages((prev) =>
          prev.map((msg) => (msg.id === assistantMsgId ? updater(msg) : msg)),
        );
      };

      try {
        const res = await fetch(`${API_BASE}/api/ask`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question,
            channel_id: options.channelId,
            session_id: sessionIdRef.current,
            mode: options.mode ?? "deep",
            attachments: options.attachments ?? [],
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
                    updateAssistant((msg) => ({
                      ...msg,
                      thinking: [...msg.thinking, data.text],
                    }));
                    break;
                  case "response_delta":
                    updateAssistant((msg) => ({
                      ...msg,
                      content: msg.content + (data.delta || ""),
                    }));
                    break;
                  case "citations":
                    updateAssistant((msg) => ({
                      ...msg,
                      citations: data.items || [],
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
                    setMessages((prev) => {
                      const updated = [...prev];
                      const lastIdx = updated.length - 1;
                      if (lastIdx >= 0 && updated[lastIdx].role === "assistant") {
                        updated[lastIdx] = {
                          ...updated[lastIdx],
                          followUps: data.suggestions ?? [],
                        };
                      }
                      return updated;
                    });
                    break;
                  case "thinking_done":
                    setMessages((prev) => {
                      const updated = [...prev];
                      const lastIdx = updated.length - 1;
                      if (lastIdx >= 0 && updated[lastIdx].role === "assistant") {
                        updated[lastIdx] = {
                          ...updated[lastIdx],
                          thinkingDuration: data.duration_ms ?? null,
                        };
                      }
                      return updated;
                    });
                    break;
                  case "error":
                    setError(data.message || "Unknown error");
                    updateAssistant((msg) => ({ ...msg, isStreaming: false }));
                    break;
                  case "done":
                    updateAssistant((msg) => {
                      const cleaned = msg.content
                        .replace(/\n*---\n*FOLLOW_UPS:\s*\[.*?\]/s, "")
                        .trimEnd();
                      return { ...msg, content: cleaned, isStreaming: false };
                    });
                    break;
                }
              } catch {
                /* skip unparseable */
              }
              currentEventType = "";
            }
          }
        }

        updateAssistant((msg) => ({ ...msg, isStreaming: false }));
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") {
          updateAssistant((msg) => ({ ...msg, isStreaming: false }));
          return;
        }
        setError(err instanceof Error ? err.message : "Unknown error");
        updateAssistant((msg) => ({ ...msg, isStreaming: false }));
      } finally {
        clearIdleTimeout();
        abortRef.current = null;
      }
    },
    [clearIdleTimeout],
  );

  const loadSession = useCallback((loaded: Message[], loadedSid: string) => {
    setMessages(loaded);
    setSessionId(loadedSid);
    sessionIdRef.current = loadedSid;
    setError(null);
  }, []);

  const retry = useCallback(() => {
    if (!lastCallRef.current) return;
    const { question, options } = lastCallRef.current;
    setMessages((prev) => {
      const withoutLastAssistant =
        prev.length > 0 && prev[prev.length - 1].role === "assistant"
          ? prev.slice(0, prev.length - 1)
          : prev;
      if (
        withoutLastAssistant.length > 0 &&
        withoutLastAssistant[withoutLastAssistant.length - 1].role === "user"
      ) {
        return withoutLastAssistant.slice(0, -1);
      }
      return withoutLastAssistant;
    });
    setError(null);
    ask(question, options);
  }, [ask]);

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
