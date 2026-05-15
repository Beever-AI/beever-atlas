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
  /** Underlying AtomicFact.id (e.g., "f_abc123"). Narrative inline
   *  citation chips look up popover content by this field, since the
   *  v3 prompt emits `[f_xxx]` markers in paragraph text. */
  fact_id?: string;
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
  page_type: "fixed" | "topic" | "sub-topic" | "folder";
  parent_id: string | null;
  section_number: string;
  content: string;
  summary: string;
  memory_count: number;
  last_updated: string;
  citations: WikiCitation[];
  children: WikiPageRef[];
  /** SHA-256 of sorted child slugs (folder pages only). Used by the
   *  maintainer to skip re-synthesis when membership is unchanged. */
  children_fingerprint?: string | null;
  /** True for planner-produced folders (vs. hand-curated, future). */
  is_synthetic?: boolean;
  /** Adaptive page module plan (``adaptive-wiki-page-content`` change).
   *  Each entry is at minimum ``{id, anchor}`` with optional per-module
   *  data payload. Empty array = legacy page; renderer falls back to
   *  the single-template flow over ``content``. */
  modules?: WikiPageModule[];
  /** Multi-section narrative article body produced by the v3
   *  ``MODULE_COMPILE_PROMPT_V3``. Mirrors the persistence-layer
   *  field. The layout reads this to mount the sticky TOC; the
   *  ``NarrativeArticleModule`` reads the same payload via its
   *  ``module.data.sections`` slice. Empty array means the page
   *  predates narrative generation OR the validator rejected the
   *  LLM output (graceful fallback to module-only rendering). */
  narrative_sections?: Array<{
    anchor: string;
    heading: string;
    paragraphs?: Array<{ text: string; citations?: string[]; is_inference?: boolean }>;
    citations?: string[];
    visual?: { kind: string; content: unknown } | null;
    citation_coverage?: number;
  }>;
  /** wiki-redesign-gap-fill / Group 4 — operator-controlled rewrite
   *  cadence. ``auto`` (default) = maintainer rewrites on every burst.
   *  ``manual`` = pages mark dirty but only flush on operator click.
   *  ``frozen`` = maintainer + Builder both skip. */
  curation_mode?: "auto" | "manual" | "frozen";
  /** True when the maintainer has marked the page dirty pending a
   *  rewrite. Used to compute the "Apply Pending Updates (N)" badge. */
  is_dirty?: boolean;
}

/** One entry in the adaptive page module plan. The ``data`` payload
 *  shape varies per module type — frontend module renderers cast it
 *  to their expected shape. */
export interface WikiPageModule {
  id: string;
  anchor: string;
  data?: Record<string, unknown>;
}

export interface WikiPageNode {
  id: string;
  title: string;
  slug: string;
  section_number: string;
  page_type: "fixed" | "topic" | "sub-topic" | "folder";
  memory_count: number;
  children: WikiPageNode[];
  /** True for planner-produced folders (vs. hand-curated, future). */
  is_synthetic?: boolean;
  /** Optional 1-2 sentence summary surfaced on the Overview's topic
   *  card grid so each card shows context without a click-through. */
  summary?: string;
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

/** One of the four pipeline phases surfaced by ``/sync/status`` (PR-3 —
 *  sync-pipeline-feedback-and-auto-wiki). The frontend renders one row
 *  per phase in the phased progress card. */
export type PhaseName =
  | "fetched"
  | "extracting"
  | "wiki_maintenance"
  | "overview_wiki";

/** Per-phase lifecycle state. ``skipped`` covers the feature-flag-off
 *  path (e.g. AUTO_OVERVIEW_WIKI=false) and the empty-channel path. */
export type PhaseState =
  | "pending"
  | "in_flight"
  | "done"
  | "skipped"
  | "failed";

export interface Phase {
  name: PhaseName;
  state: PhaseState;
  /** Number of items completed for this phase (e.g. messages fetched,
   *  facts extracted, pages refreshed). Optional — phases like
   *  ``overview_wiki`` are boolean-shaped and omit it. */
  done?: number;
  /** Total items expected for this phase. Optional — same caveat as
   *  ``done``. */
  total?: number;
  duration_ms?: number;
  /** Last human-readable label emitted while this phase was active.
   *  Used by the activity feed when ``recent_events`` is not yet
   *  populated. */
  last_event_label?: string;
  /** ISO-8601 UTC timestamp of when this phase's current attempt
   *  started. Currently emitted for ``overview_wiki`` in the
   *  ``in_flight`` state so the WikiTab can render an elapsed-time
   *  stamp and a Retry button if the build hangs. */
  started_at?: string;
}

export interface RecentEvent {
  /** ISO timestamp (UTC) when the event was emitted by the worker. */
  ts: string;
  /** Pipeline stage tag — one of ``fetch``, ``preprocess``,
   *  ``extract_facts``, ``extract_entities``, ``embed``, ``validate``,
   *  ``persist``, ``wiki_maintenance``, ``overview_wiki``, ... */
  stage: string;
  label: string;
  /** unified-llm-wiki-graph-redesign — structured event taxonomy slot.
   *  ``message_processing`` / ``agent_state`` / ``wiki_update`` /
   *  ``cost_summary`` / ``parse_failure`` / ``legacy``. */
  event_type?: string;
  /** Event-type-specific structured data the SyncMonitor consumes.
   *  Always optional; legacy emitters omit it. */
  payload?: Record<string, unknown> | null;
}

export interface ParseFailureState {
  count_last_10_min: number;
  threshold: number;
  should_show_banner: boolean;
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
  /** PR-3 — phased progress feedback. When present, the new
   *  ``PhasedProgressCard`` replaces the legacy decoupled-mode widget.
   *  Absent on responses from older backends — the legacy
   *  ``ExtractionWorkerPanel`` is the fallback. */
  phases?: Phase[];
  /** Last ~10 pipeline events from the worker's in-memory ring buffer. */
  recent_events?: RecentEvent[];
  /** EWMA-smoothed seconds-remaining estimate. ``null`` until enough
   *  samples (>=3) accumulate; ``undefined`` on legacy backends. */
  smoothed_eta_seconds?: number | null;
  /** Failed rows still inside their backoff window — will be retried. */
  retrying?: number;
  /** Failed rows past ``max_retries`` — will NOT be retried. */
  abandoned?: number;
  /** unified-llm-wiki-graph-redesign — wiki-side parse-failure state
   *  feeding the WikiTab banner + SyncMonitor footer. ``undefined``
   *  on legacy backends. */
  parse_failure_state?: ParseFailureState;
}

export interface BatchResultEntry {
  batch_num: number;
  facts_count: number;
  entities_count: number;
  relationships_count: number;
  /** Embeddings produced in this batch (jina/etc). Optional — present
   *  when derived from activity_log; absent on legacy backends. */
  embedded_count?: number;
  /** Media items enriched by the preprocessor (images, PDFs, audio). */
  media_count?: number;
  /** Lifecycle state — populated when caller has BatchSummary context.
   *  ``running`` means stage_start was seen but no persister stage_output
   *  yet; ``done`` means persister wrote successfully; ``pending`` is
   *  pre-claim; ``failed`` is explicit error. */
  state?: "pending" | "running" | "done" | "failed";
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
  platform: "slack" | "discord" | "teams" | "telegram" | "mattermost" | "file";
  display_name: string;
  status: "connected" | "disconnected" | "error";
  error_message: string | null;
  selected_channels: string[];
  source: "ui" | "env";
  created_at: string;
  updated_at: string;
}

export interface PlatformCredentials {
  platform: "slack" | "discord" | "teams" | "telegram" | "mattermost";
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

export type WikiMaintenanceMode = "auto" | "manual" | "inherit";

export interface WikiConfig {
  enabled: boolean | null;
  generation_strategy: WikiGenerationStrategy | null;
  cron_expression: string | null;
  auto_regenerate_on_stale: boolean | null;
  min_facts_for_generation: number | null;
  topic_subpage_threshold: number | null;
  maintenance_mode?: WikiMaintenanceMode | null;
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

// --- Embedding settings (PR-E / PR-F) ---

export type EmbeddingProvider =
  | "jina_ai"
  | "openai"
  | "cohere"
  | "voyage"
  | "gemini"
  | "mistral"
  | "ollama"
  | "bedrock"
  | "vertex_ai";

export interface EmbeddingSettings {
  provider: EmbeddingProvider;
  model: string;
  dimensions: number;
  rpm: number;
  api_base: string;
  task: string;
  has_api_key: boolean;
  api_key_masked: string;
  source: "db" | "env" | "default";
  dim_guard_enabled: boolean;
  last_probe_at: string | null;
  last_probe_ok: boolean | null;
  last_probe_error: string | null;
  // Surface the persisted (Weaviate-current) embedding meta so the UI
  // can show a "Re-embed required" banner when configured config !=
  // what's actually in storage.
  persisted_provider: string | null;
  persisted_model: string | null;
  persisted_dimensions: number | null;
  fact_count: number | null;
  migration_required: boolean;
}

export interface EmbeddingUpdateRequest {
  provider?: string;
  model?: string;
  dimensions?: number;
  rpm?: number;
  api_base?: string;
  task?: string;
  api_key?: string;
  confirm_migration?: boolean;
}

export interface EmbeddingProbeResult {
  ok: boolean;
  dimensions: number | null;
  latency_ms: number | null;
  provider: string;
  model: string;
  error: string | null;
}

export interface EmbeddingMigrationStatus {
  running: boolean;
  job_id: string | null;
  stage: string | null;
  processed: number | null;
  total: number | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

/**
 * ``GET /api/settings/embedding-migration/state`` (PR6) — dim-mismatch
 * detection driven by the ``embedding`` Assignment (desired) vs the
 * ``embedding_meta`` checkpoint (persisted, i.e. what's actually in storage).
 * ``reembed_supported`` is false when the Assignment's endpoint resolves to a
 * provider the legacy re-embed job can't drive (e.g. an Anthropic endpoint);
 * ``reason`` explains why so the UI can disable "Start re-embed" helpfully.
 */
export interface EmbeddingReembedState {
  migration_required: boolean;
  desired_provider: string | null;
  desired_model: string | null;
  desired_dimensions: number | null;
  persisted_provider: string | null;
  persisted_model: string | null;
  persisted_dimensions: number | null;
  fact_count: number | null;
  reembed_supported: boolean;
  reason: string | null;
}

/** ``POST /api/settings/embedding-migration/spawn`` (PR6) response. */
export interface EmbeddingReembedSpawnResponse {
  job_id: string;
  status: string; // "running" | "running_existing"
}
