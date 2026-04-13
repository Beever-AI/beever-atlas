import { useState, useCallback, useRef, useEffect } from "react";
import type { Message, MessageCitations, AskMetadata, ToolCallEvent, AnswerMode, AttachmentFile, DecompositionPlan } from "../types/askTypes";
import type { ToolDescriptor } from "../types/toolTypes";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

// ---------------------------------------------------------------------------
// Tool descriptor cache — module-scoped so subsequent hook mounts reuse it.
// ---------------------------------------------------------------------------

let _toolDescriptorCache: ToolDescriptor[] | null = null;
let _toolDescriptorPromise: Promise<ToolDescriptor[]> | null = null;

function fetchToolDescriptors(): Promise<ToolDescriptor[]> {
  if (_toolDescriptorCache !== null) return Promise.resolve(_toolDescriptorCache);
  if (_toolDescriptorPromise !== null) return _toolDescriptorPromise;
  _toolDescriptorPromise = fetch(`${API_BASE}/api/ask/tools`)
    .then((res) => {
      if (!res.ok) throw new Error(`GET /api/ask/tools returned ${res.status}`);
      return res.json() as Promise<{ tools: ToolDescriptor[] }>;
    })
    .then((body) => {
      _toolDescriptorCache = body.tools ?? [];
      return _toolDescriptorCache;
    })
    .catch((err) => {
      console.error("[useAskSession] Failed to load tool descriptors:", err);
      _toolDescriptorPromise = null; // allow retry on next mount
      return [];
    });
  return _toolDescriptorPromise;
}

// ---------------------------------------------------------------------------
// localStorage helpers for disabledTools persistence
// ---------------------------------------------------------------------------

function localKey(conversationId: string): string {
  return `askSession.disabledTools.${conversationId}`;
}

function loadDisabledTools(conversationId: string): string[] {
  try {
    const raw = localStorage.getItem(localKey(conversationId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveDisabledTools(conversationId: string, tools: string[]): void {
  try {
    localStorage.setItem(localKey(conversationId), JSON.stringify(tools));
  } catch {
    // quota exceeded or private browsing — silently ignore
  }
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
  citations: MessageCitations;
  metadata: AskMetadata | null;
  toolCalls: ToolCallEvent[];
  disabledTools: string[];
  toggleTool: (name: string) => void;
  toolDescriptors: ToolDescriptor[];
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
  // Typewriter controllers — reset per ask call
  const contentTwRef = useRef<TypewriterController | null>(null);
  const thinkingTwRef = useRef<TypewriterController | null>(null);

  // Tool descriptors — fetched once, cached at module scope
  const [toolDescriptors, setToolDescriptors] = useState<ToolDescriptor[]>([]);
  // disabledTools — per-conversation, persisted to localStorage
  const [disabledTools, setDisabledTools] = useState<string[]>([]);

  // Load tool descriptors on first mount
  useEffect(() => {
    fetchToolDescriptors().then(setToolDescriptors);
  }, []);

  // Load persisted disabledTools when conversationId becomes known
  useEffect(() => {
    if (sessionIdRef.current) {
      setDisabledTools(loadDisabledTools(sessionIdRef.current));
    }
  }, [sessionId]);

  const toggleTool = useCallback((name: string) => {
    const convId = sessionIdRef.current;
    setDisabledTools((prev) => {
      const next = prev.includes(name)
        ? prev.filter((n) => n !== name)
        : [...prev, name];
      if (convId) saveDisabledTools(convId, next);
      return next;
    });
  }, []);

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
        const res = await fetch(`${API_BASE}/api/ask`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question,
            channel_id: options.channelId,
            session_id: sessionIdRef.current,
            mode: options.mode ?? "deep",
            attachments: options.attachments ?? [],
            disabled_tools: disabledTools,
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
                    contentTwRef.current?.cancel();
                    thinkingTwRef.current?.cancel();
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
    [clearIdleTimeout, disabledTools],
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
    disabledTools,
    toggleTool,
    toolDescriptors,
  };
}
