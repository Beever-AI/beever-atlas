// Types for the Ask tab SSE events and tool call UI

export type AnswerMode = "quick" | "deep" | "summarize";
export type FeedbackRating = "up" | "down";

export interface AttachmentFile {
  file_id: string;
  filename: string;
  extracted_text: string;
  mime_type: string;
  size_bytes: number;
  /** Transient client-only flag: true while the POST /upload is in flight.
   *  Pending entries have a temp `file_id` prefixed with `pending-`; the
   *  composer renders them with a spinner and disables Send.  Finalized
   *  entries (from the server response) drop this flag. */
  uploading?: boolean;
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

// ----- Phase 2 structured citation contract -----

export type SourceKind =
  | "channel_message"
  | "wiki_page"
  | "qa_history"
  | "uploaded_file"
  | "web_result"
  | "graph_relationship"
  | "decision_record"
  | "media";

export type MediaKind =
  | "image"
  | "pdf"
  | "video"
  | "audio"
  | "link_preview"
  | "document";

export interface MediaAttachment {
  kind: MediaKind;
  url: string;
  thumbnail_url?: string;
  mime_type?: string;
  filename?: string;
  title?: string;
  alt_text?: string;
  width?: number;
  height?: number;
  byte_size?: number;
}

export interface SourceRetrievedBy {
  tool?: string;
  query?: string;
  score?: number | null;
}

export interface Source {
  id: string;
  kind: SourceKind;
  title: string;
  excerpt: string;
  retrieved_by: SourceRetrievedBy;
  native: Record<string, unknown>;
  attachments: MediaAttachment[];
  permalink: string | null;
  created_at?: string;
}

export interface CitationRef {
  marker: number;
  source_id: string;
  inline?: boolean;
  ranges?: Array<{ start: number; end: number }>;
  note?: string | null;
}

/** Full envelope shape the backend ships. Legacy messages may only have `items`. */
export interface CitationEnvelope {
  items: Citation[];
  sources: Source[];
  refs: CitationRef[];
}

/** Shape `message.citations` can take in memory after normalization. */
export type MessageCitations = Citation[] | CitationEnvelope;

export interface AskRequest {
  question: string;
  channel_id?: string;
  session_id?: string;
  mode?: AnswerMode;
  attachments?: AttachmentFile[];
  disabled_tools?: string[];
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

export interface DecompositionSubQuery {
  label: string;
  query: string;
}

export interface DecompositionPlan {
  internal: DecompositionSubQuery[];
  external: DecompositionSubQuery[];
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  /**
   * Citations carried by an assistant turn. Legacy shape is `Citation[]`
   * (flat list); Phase 2 / registry-enabled path uses `CitationEnvelope`
   * which preserves structured `sources` + `refs` alongside `items` for
   * back-compat.
   */
  citations: MessageCitations;
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
  /** Query decomposition plan, present only for complex multi-part questions. */
  decomposition?: DecompositionPlan;
}
