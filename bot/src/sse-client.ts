/**
 * SSE client for consuming the backend /api/channels/:id/ask stream.
 * Accumulates response_delta events and extracts citations + metadata.
 */

import type { AskResult } from "./index.js";

interface SSEEvent {
  type: string;
  data: Record<string, unknown>;
}

function parseSSEEvents(text: string): SSEEvent[] {
  const events: SSEEvent[] = [];
  let currentType = "";

  for (const line of text.split("\n")) {
    if (line.startsWith("event: ")) {
      currentType = line.slice(7);
    } else if (line.startsWith("data: ") && currentType) {
      try {
        const data = JSON.parse(line.slice(6)) as Record<string, unknown>;
        events.push({ type: currentType, data });
      } catch {
        // Skip unparseable data lines
      }
      currentType = "";
    }
  }

  return events;
}

export async function consumeSSEStream(response: Response): Promise<AskResult> {
  const text = await response.text();
  const events = parseSSEEvents(text);

  let answer = "";
  let citations: Array<{ type: string; text: string }> = [];
  let route = "echo";
  let confidence = 0;
  let costUsd = 0;

  for (const event of events) {
    switch (event.type) {
      case "response_delta":
        answer += (event.data.delta as string) || "";
        break;
      case "citations":
        citations = (event.data.items as Array<{ type: string; text: string }>) || [];
        break;
      case "metadata":
        route = (event.data.route as string) || "echo";
        confidence = (event.data.confidence as number) || 0;
        costUsd = (event.data.cost_usd as number) || 0;
        break;
      case "error":
        throw new Error((event.data.message as string) || "Unknown backend error");
    }
  }

  return { answer, citations, route, confidence, costUsd };
}
