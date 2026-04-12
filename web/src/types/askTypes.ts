// Types for the Ask tab SSE events and tool call UI

export type AnswerMode = "quick" | "deep" | "summarize";
export type FeedbackRating = "up" | "down";

export interface AttachmentFile {
  file_id: string;
  filename: string;
  extracted_text: string;
  mime_type: string;
  size_bytes: number;
}

export interface FollowUpSuggestion {
  text: string;
}

export interface ThinkingState {
  tokens: string[];
  isThinking: boolean;
  durationMs: number | null;
}

export interface FeedbackState {
  rating: FeedbackRating | null;
  comment?: string;
}

export interface Citation {
  type: string;
  text: string;
  author?: string;
  channel?: string;
  timestamp?: string;
  permalink?: string;
}

export interface AskMetadata {
  route: string;
  confidence: number;
  cost_usd: number;
  channel_id?: string;
  session_id?: string;
  mode?: AnswerMode;
}

export interface ToolCallStartPayload {
  tool_name: string;
  input: Record<string, unknown>;
}

export interface ToolCallEndPayload {
  tool_name: string;
  result_summary: string;
  latency_ms: number;
  facts_found: number;
}

export interface ToolCallEvent {
  tool_name: string;
  input: Record<string, unknown>;
  result_summary?: string;
  latency_ms?: number;
  facts_found?: number;
  status: "running" | "done" | "error";
  started_at: number; // Date.now() timestamp
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  toolCalls: ToolCallEvent[];
  thinking: string[];
  metadata: AskMetadata | null;
  isStreaming: boolean;
  feedback?: FeedbackState;
  attachments?: AttachmentFile[];
  followUps?: string[];
  thinkingDuration?: number | null;
  mode?: AnswerMode;
  /** Channel this turn queried (v2 schema). Absent on legacy messages. */
  channel_id?: string;
}
