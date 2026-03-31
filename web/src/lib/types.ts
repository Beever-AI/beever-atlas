export interface ComponentHealth {
  status: "up" | "down";
  latency_ms: number;
  error: string | null;
}

export interface HealthResponse {
  status: "healthy" | "degraded" | "unhealthy";
  components: Record<string, ComponentHealth>;
  checked_at: string;
}

export interface Citation {
  id: string;
  type: "fact" | "graph" | "message";
  fact_text?: string;
  quality_score?: number;
  tier?: "atomic" | "topic" | "summary";
  graph_path?: string;
  entities?: { name: string; type: string }[];
  channel: string;
  user: string;
  timestamp: string;
  permalink: string;
}

export interface AskResponse {
  answer: string;
  citations: Citation[];
  route_used: "semantic" | "graph" | "both";
  confidence: number;
  degraded: boolean;
  cost_usd: number;
}

export interface WikiResponse {
  content: string;
  generated_at: string;
  is_stale: boolean;
  channel_id: string;
}

export interface SyncResponse {
  job_id: string;
  status: "started";
}

export interface SyncStatusResponse {
  state: "idle" | "syncing" | "error";
  job_id?: string;
  total_messages?: number;
  processed_messages?: number;
  current_batch?: number;
  current_stage?: string | null;
  stage_timings?: Record<string, number>;
  stage_details?: Record<string, Record<string, unknown>>;
  errors?: string[];
  started_at?: string | null;
  completed_at?: string | null;
}

export interface ChannelInfo {
  channel_id: string;
  name: string;
  platform: "slack" | "teams" | "discord";
  is_private: boolean;
  last_synced_at: string | null;
  message_count: number;
  memory_count: number;
  entity_count: number;
  wiki_is_stale: boolean;
  sync_status: "idle" | "running" | "failed";
}

export interface TopicCluster {
  id: string;
  summary: string;
  topic_tags: string[];
  member_count: number;
}

export interface AtomicFact {
  id: string;
  memory: string;
  quality_score: number;
  timestamp: string;
  user_name: string;
  topic_tags: string[];
  entity_tags: string[];
  importance: string;
  permalink: string;
}

export interface MemoryTier0 {
  channel_id: string;
  channel_name: string;
  summary: string;
  updated_at: string;
  message_count: number;
}

export interface MemoryTier1 {
  id: string;
  topic: string;
  summary: string;
  fact_count: number;
  date_range: { start: string; end: string };
  topic_tags: string[];
}

export interface MemoryTier2 {
  id: string;
  memory_text: string;
  quality_score: number;
  tier: string;
  cluster_id: string | null;
  channel_id: string;
  platform: string;
  author_id: string;
  author_name: string;
  message_ts: string;
  thread_ts: string | null;
  source_message_id: string;
  topic_tags: string[];
  entity_tags: string[];
  action_tags: string[];
  importance: string;
  graph_entity_ids: string[];
  source_media_url: string;
  source_media_type: string; // "image" | "pdf" | "doc" | "video" | ""
  valid_at: string | null;
  invalid_at: string | null;
}
