/**
 * Structured, PII-free observability for sent replies.
 *
 * Emits one `reply_sent` record per answered request so we can see — per
 * platform and surface — latency, route, empty rate, and how often citations /
 * follow-ups are present. Deliberately carries NO thread id, author id, question
 * text, or answer text, so logs are safe to ship.
 */
import { logger } from "./logger.js";

export type ReplySurface = "mention" | "follow-up" | "dm";

export interface ReplyMetric {
  event: "reply_sent";
  surface: ReplySurface;
  platform: string;
  route: string;
  latencyMs: number;
  isEmpty: boolean;
  citationCount: number;
  followUpCount: number;
}

export interface ReplyMetricInput {
  surface: ReplySurface;
  platform: string;
  route: string;
  latencyMs: number;
  isEmpty: boolean;
  citationCount: number;
  followUpCount: number;
}

export function buildReplyMetric(input: ReplyMetricInput): ReplyMetric {
  return {
    event: "reply_sent",
    surface: input.surface,
    platform: input.platform,
    route: input.route,
    latencyMs: Math.max(0, Math.round(input.latencyMs)),
    isEmpty: input.isEmpty,
    citationCount: Math.max(0, input.citationCount),
    followUpCount: Math.max(0, input.followUpCount),
  };
}

export function logReplySent(
  input: ReplyMetricInput,
  sink: (m: ReplyMetric) => void = (m) => logger.info(JSON.stringify(m)),
): void {
  sink(buildReplyMetric(input));
}
