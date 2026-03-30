import { useState, useCallback, useRef } from "react";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

interface Citation {
  type: string;
  text: string;
}

interface AskMetadata {
  route: string;
  confidence: number;
  cost_usd: number;
  channel_id?: string;
}

interface UseAskReturn {
  ask: (question: string) => Promise<void>;
  response: string;
  thinking: string[];
  citations: Citation[];
  metadata: AskMetadata | null;
  isStreaming: boolean;
  error: string | null;
  reset: () => void;
}

export function useAsk(channelId: string): UseAskReturn {
  const [response, setResponse] = useState("");
  const [thinking, setThinking] = useState<string[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [metadata, setMetadata] = useState<AskMetadata | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const reset = useCallback(() => {
    setResponse("");
    setThinking([]);
    setCitations([]);
    setMetadata(null);
    setError(null);
    setIsStreaming(false);
  }, []);

  const ask = useCallback(
    async (question: string) => {
      // Cancel any in-progress request
      if (abortRef.current) {
        abortRef.current.abort();
      }

      const controller = new AbortController();
      abortRef.current = controller;

      reset();
      setIsStreaming(true);

      try {
        const res = await fetch(
          `${API_BASE}/api/channels/${channelId}/ask`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question }),
            signal: controller.signal,
          },
        );

        if (!res.ok) {
          throw new Error(`Server returned ${res.status}`);
        }

        const reader = res.body?.getReader();
        if (!reader) throw new Error("No response body");

        const decoder = new TextDecoder();
        let buffer = "";
        let currentEventType = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

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
                    setThinking((prev) => [...prev, data.text]);
                    break;
                  case "response_delta":
                    setResponse((prev) => prev + (data.delta || ""));
                    break;
                  case "citations":
                    setCitations(data.items || []);
                    break;
                  case "metadata":
                    setMetadata(data);
                    break;
                  case "error":
                    setError(data.message || "Unknown error");
                    break;
                  case "done":
                    break;
                }
              } catch {
                // Skip unparseable lines
              }
              currentEventType = "";
            }
          }
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") {
          return; // User cancelled
        }
        setError(err instanceof Error ? err.message : "Unknown error");
      } finally {
        setIsStreaming(false);
        abortRef.current = null;
      }
    },
    [channelId, reset],
  );

  return { ask, response, thinking, citations, metadata, isStreaming, error, reset };
}
