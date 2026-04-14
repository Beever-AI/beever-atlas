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

export interface WikiCitation {
  id: string;
  author: string;
  channel: string;
  timestamp: string;
  text_excerpt: string;
  permalink: string;
  media_type?: "pdf" | "image" | "link" | "video" | "audio";
  media_name?: string;
}

export interface WikiPageRef {
  id: string;
  title: string;
  slug: string;
  section_number: string;
  memory_count: number;
}

export interface WikiPage {
  id: string;
  slug: string;
  title: string;
  page_type: "fixed" | "topic" | "sub-topic";
  parent_id: string | null;
  section_number: string;
  content: string;
  summary: string;
  memory_count: number;
  last_updated: string;
  citations: WikiCitation[];
  children: WikiPageRef[];
}

export interface WikiPageNode {
  id: string;
  title: string;
  slug: string;
  section_number: string;
  page_type: "fixed" | "topic" | "sub-topic";
  memory_count: number;
  children: WikiPageNode[];
}

export interface WikiStructure {
  channel_id: string;
  channel_name: string;
  platform: string;
  generated_at: string;
  is_stale: boolean;
  pages: WikiPageNode[];
}

export interface WikiMetadata {
  member_count: number;
  message_count: number;
  memory_count: number;
  entity_count: number;
  media_count: number;
  page_count: number;
  generation_cost_usd: number;
  generation_duration_ms: number;
}

export interface WikiResponse {
  channel_id: string;
  channel_name: string;
  platform: string;
  generated_at: string;
  is_stale: boolean;
  structure: WikiStructure;
  overview: WikiPage;
  metadata: WikiMetadata;
  version_count: number;
}

export interface WikiVersionSummary {
  version_number: number;
  channel_id: string;
  /** BCP-47 tag of the language this version was rendered in. Missing on
   *  docs archived before multilang tagging was introduced. */
  target_lang?: string;
  generated_at: string;
  archived_at: string;
  page_count: number;
  model: string;
}

export interface WikiVersionResponse {
  version_number: number;
  channel_id: string;
  channel_name: string;
  platform: string;
  target_lang?: string;
  generated_at: string;
  archived_at: string;
  page_count: number;
  model: string;
  structure: WikiStructure;
  overview: WikiPage;
  pages: Record<string, WikiPage>;
  metadata: WikiMetadata;
}

export interface SyncResponse {
  job_id: string;
  status: "started";
}

export interface ActivitySample {
  item_type: string;
  content?: string;
  agent?: string;
  author?: string;
  tags?: string[];
  score?: number;
  source?: string;
  target?: string;
  rel_type?: string;
  model?: string;
  status?: string;
}

export interface ActivityEntry {
  type: "stage_start" | "stage_output";
  agent: string;
  stage?: string;
  message?: string;
  metrics?: Record<string, number>;
  samples?: ActivitySample[];
  elapsed?: number;
  model?: string;
  /** Present under concurrent batching; identifies which batch emitted this entry. */
  batch_idx?: number;
}

export interface SyncStatusResponse {
  state: "idle" | "syncing" | "error";
  job_id?: string;
  total_messages?: number;
  parent_messages?: number;
  processed_messages?: number;
  current_batch?: number;
  total_batches?: number;
  batches_completed?: number;
  current_stage?: string | null;
  stage_timings?: Record<string, number>;
  stage_details?: {
    activity_log?: ActivityEntry[];
    /** Per-batch current stage labels keyed by batch_idx (string). Present only under concurrent batching. */
    batch_stages?: Record<string, string>;
    [key: string]: unknown;
  };
  batch_results?: BatchResultEntry[];
  batch_job_state?: string | null;
  batch_job_elapsed_seconds?: number | null;
  errors?: string[];
  started_at?: string | null;
  completed_at?: string | null;
}

export interface BatchResultEntry {
  batch_num: number;
  facts_count: number;
  entities_count: number;
  relationships_count: number;
  sample_facts: string[];
  sample_entities: { name: string; type: string }[];
  sample_relationships: { source: string; target: string; type: string }[];
  duration_seconds: number;
  error: string | null;
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
  title: string;
  summary: string;
  current_state: string;
  open_questions: string;
  impact_note: string;
  topic_tags: string[];
  member_count: number;
  key_entities: Array<{ id: string; name: string; type: string }>;
  key_relationships: Array<{ source: string; type: string; target: string; confidence: string }>;
  date_range_start: string;
  date_range_end: string;
  authors: string[];
  media_refs: string[];
  link_refs: string[];
  high_importance_count: number;
  related_cluster_ids: string[];
  staleness_score: number;
  status: string;
  fact_type_counts: Record<string, number>;
  key_facts: Array<{
    fact_id: string;
    memory_text: string;
    author_name: string;
    message_ts: string;
    fact_type: string;
    importance: string;
    quality_score: number;
    source_message_id: string;
  }>;
  decisions: Array<{
    name: string;
    decided_by: string;
    status: string;
    superseded_by: string;
    date: string;
    context: string;
  }>;
  people: Array<{ name: string; role: string; entity_id: string }>;
  technologies: Array<{ name: string; category: string; champion: string }>;
  projects: Array<{ name: string; status: string; owner: string; blockers: string[] }>;
  faq_candidates: Array<{ question: string; answer: string }>;
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
  description: string;
  themes: string;
  momentum: string;
  team_dynamics: string;
  updated_at: string;
  message_count: number;
  cluster_count: number;
  author_count: number;
  media_count: number;
  worst_staleness: number;
  top_people: Array<{ name: string; role: string; topic_count: number; expertise_topics: string[] }>;
  tech_stack: Array<{ name: string; category: string; champion: string; topic_count: number }>;
  glossary_terms: Array<{ term: string; definition: string; first_mentioned_by: string; related_topics: string[] }>;
  recent_activity_summary: {
    facts_added_7d: number;
    decisions_added_7d: number;
    new_topics: string[];
    updated_topics: string[];
    highlights: Array<{ memory_text: string; author_name: string; fact_type: string }>;
  } | null;
}

export interface MemoryTier1 {
  id: string;
  title: string;
  topic: string;
  summary: string;
  current_state: string;
  open_questions: string;
  impact_note: string;
  fact_count: number;
  date_range: { start: string; end: string };
  topic_tags: string[];
  authors: string[];
  status: string;
  staleness_score: number;
  key_facts: Array<{
    fact_id: string;
    memory_text: string;
    author_name: string;
    message_ts: string;
    fact_type: string;
    importance: string;
    quality_score: number;
  }>;
  people: Array<{ name: string; role: string }>;
  decisions: Array<{ name: string; decided_by: string; status: string; superseded_by: string }>;
  technologies: Array<{ name: string; category: string; champion: string }>;
  faq_candidates: Array<{ question: string; answer: string }>;
  fact_type_counts: Record<string, number>;
}

export interface PlatformConnection {
  id: string;
  platform: "slack" | "discord" | "teams" | "telegram" | "file";
  display_name: string;
  status: "connected" | "disconnected" | "error";
  error_message: string | null;
  selected_channels: string[];
  source: "ui" | "env";
  created_at: string;
  updated_at: string;
}

export interface PlatformCredentials {
  platform: "slack" | "discord" | "teams" | "telegram";
  credentials: Record<string, string>;
  display_name?: string;
}

// --- File Import ---

export interface ImportColumnMapping {
  content: string;
  author?: string | null;
  author_name?: string | null;
  timestamp?: string | null;
  timestamp_time?: string | null;
  message_id?: string | null;
  thread_id?: string | null;
  attachments?: string | null;
  reactions?: string | null;
}

export interface ImportPreviewResponse {
  file_id: string;
  filename: string;
  encoding: string;
  format: "csv" | "tsv" | "jsonl";
  row_count_estimate: number;
  headers: string[];
  sample_messages: Array<{
    content: string;
    author: string;
    author_name: string;
    timestamp: string;
  }>;
  mapping: ImportColumnMapping;
  mapping_source: "preset" | "fuzzy" | "llm" | "fuzzy_fallback";
  preset: string | null;
  overall_confidence: number;
  per_field_confidence: Record<string, number>;
  needs_review: boolean;
  detected_source: string | null;
  notes: string;
  expires_at: string;
}

export interface ImportCommitRequest {
  file_id: string;
  channel_name: string;
  channel_id?: string;
  mapping: ImportColumnMapping;
  skip_empty?: boolean;
  skip_system?: boolean;
  skip_deleted?: boolean;
  dayfirst?: boolean;
  max_rows?: number;
}

export interface ImportCommitResponse {
  job_id: string;
  channel_id: string;
  channel_name: string;
  connection_id: string;
  total_messages: number;
  status: string;
}

export interface AvailableChannel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  topic: string | null;
  purpose: string | null;
  connection_id: string | null;
}

export interface FavoriteChannel {
  channel_id: string;
  connection_id: string | null;
}

export interface WorkspaceGroup {
  connection: PlatformConnection;
  channels: AvailableChannel[];
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
  source_media_urls: string[];
  source_link_urls: string[];
  source_link_titles: string[];
  source_link_descriptions: string[];
  valid_at: string | null;
  invalid_at: string | null;
  fact_type: string;
  superseded_by: string | null;
  thread_context_summary: string;
  source_media_names: string[];
}

export interface ChannelSummaryResponse {
  text: string;
  cluster_count: number;
  fact_count: number;
  channel_name: string;
  description: string;
  themes: string;
  momentum: string;
  team_dynamics: string;
  key_decisions: Array<Record<string, unknown>>;
  key_entities: Array<Record<string, unknown>>;
  key_topics: Array<Record<string, unknown>>;
  date_range_start: string;
  date_range_end: string;
  media_count: number;
  author_count: number;
  worst_staleness: number;
  top_decisions: Array<Record<string, unknown>>;
  top_people: Array<{ name: string; role: string; topic_count: number; expertise_topics: string[] }>;
  tech_stack: Array<{ name: string; category: string; champion: string; topic_count: number }>;
  active_projects: Array<{ name: string; status: string; owner: string; blockers: string[] }>;
  glossary_terms: Array<{ term: string; definition: string; first_mentioned_by: string; related_topics: string[] }>;
  recent_activity_summary: {
    facts_added_7d: number;
    decisions_added_7d: number;
    entities_added_7d: number;
    new_topics: string[];
    updated_topics: string[];
    highlights: Array<{ memory_text: string; author_name: string; fact_type: string; message_ts: string }>;
  };
  topic_graph_edges: Array<{ source_cluster_id: string; target_cluster_id: string; source_title: string; target_title: string; shared_entities: string[] }>;
}

export interface ConsolidateResponse {
  status: string;
  channel_id: string;
}

// --- Sync Policy Types ---

export type SyncTriggerMode = "manual" | "interval" | "cron" | "webhook";
export type ConsolidationStrategy = "after_every_sync" | "after_n_syncs" | "scheduled" | "manual";

export interface SyncConfig {
  trigger_mode: SyncTriggerMode | null;
  cron_expression: string | null;
  interval_minutes: number | null;
  sync_type: "auto" | "full" | "incremental" | null;
  max_messages: number | null;
  min_sync_interval_minutes: number | null;
}

export interface IngestionConfig {
  batch_size: number | null;
  quality_threshold: number | null;
  max_facts_per_message: number | null;
  skip_entity_extraction: boolean | null;
  skip_graph_writes: boolean | null;
}

export interface ConsolidationConfig {
  strategy: ConsolidationStrategy | null;
  after_n_syncs: number | null;
  cron_expression: string | null;
  similarity_threshold: number | null;
  merge_threshold: number | null;
  min_facts_for_clustering: number | null;
  staleness_refresh_days: number | null;
}

export type WikiGenerationStrategy = "after_every_sync" | "after_consolidation" | "scheduled" | "manual";

export interface WikiConfig {
  enabled: boolean | null;
  generation_strategy: WikiGenerationStrategy | null;
  cron_expression: string | null;
  auto_regenerate_on_stale: boolean | null;
  min_facts_for_generation: number | null;
  topic_subpage_threshold: number | null;
}

export interface ChannelPolicyResponse {
  channel_id: string;
  preset: string | null;
  policy: {
    sync: SyncConfig;
    ingestion: IngestionConfig;
    consolidation: ConsolidationConfig;
    wiki: WikiConfig;
  } | null;
  effective: {
    sync: SyncConfig;
    ingestion: IngestionConfig;
    consolidation: ConsolidationConfig;
    wiki: WikiConfig;
  };
  enabled: boolean;
  syncs_since_last_consolidation: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface GlobalDefaultsResponse {
  sync: SyncConfig;
  ingestion: IngestionConfig;
  consolidation: ConsolidationConfig;
  wiki: WikiConfig;
  max_concurrent_syncs: number;
  updated_at: string;
}

export interface PolicyPreset {
  id: string;
  name: string;
  description: string;
  sync: SyncConfig;
  ingestion: IngestionConfig;
  consolidation: ConsolidationConfig;
  wiki: WikiConfig;
}

// --- Agent Model Configuration ---

export interface AgentModelConfig {
  models: Record<string, string>;
  defaults: Record<string, string>;
  updated_at: string | null;
}

export interface AvailableModels {
  gemini: string[];
  ollama: string[];
  ollama_connected: boolean;
}

export type ModelPreset = "balanced" | "cost_optimized" | "quality_first" | "local_first";

// --- Sync History ---

export interface SyncHistoryEntry {
  job_id: string;
  status: "running" | "completed" | "failed";
  sync_type: "full" | "incremental";
  total_messages: number;
  parent_messages: number;
  processed_messages: number;
  total_batches: number;
  current_stage: string | null;
  stage_timings: Record<string, number>;
  stage_details: {
    activity_log?: ActivityEntry[];
    batch_stages?: Record<string, string>;
    [key: string]: unknown;
  };
  batch_results: BatchResultEntry[];
  errors: string[];
  started_at: string | null;
  completed_at: string | null;
}
