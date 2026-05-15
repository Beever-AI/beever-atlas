export type AgentGroup =
  | "ingestion"
  | "media"
  | "post_processing"
  | "wiki"
  | "qa"
  | "utility";

export interface AgentMeta {
  name: string;
  displayName: string;
  description: string;
  group: AgentGroup;
}

export const AGENT_META: AgentMeta[] = [
  // Ingestion Pipeline
  { name: "fact_extractor", displayName: "Fact Extractor", description: "Extracts discrete facts from messages", group: "ingestion" },
  { name: "entity_extractor", displayName: "Entity Extractor", description: "Identifies people, projects, and tools", group: "ingestion" },
  { name: "cross_batch_validator", displayName: "Cross-Batch Validator", description: "Deduplicates and validates entities across batches", group: "ingestion" },
  { name: "coreference_resolver", displayName: "Coreference Resolver", description: "Resolves pronouns to explicit entity names", group: "ingestion" },

  // Media Processing
  { name: "image_describer", displayName: "Image Describer", description: "Generates text descriptions of images", group: "media" },
  { name: "video_analyzer", displayName: "Video Analyzer", description: "Transcribes and describes video content", group: "media" },
  { name: "audio_transcriber", displayName: "Audio Transcriber", description: "Transcribes audio files to text", group: "media" },
  { name: "document_digester", displayName: "Document Digester", description: "Summarizes lengthy documents to Markdown bullet points", group: "media" },

  // Post-Processing
  { name: "contradiction_detector", displayName: "Contradiction Detector", description: "Detects conflicting facts for supersession", group: "post_processing" },
  { name: "summarizer", displayName: "Summarizer", description: "Generates topic and channel summaries", group: "post_processing" },

  // Wiki Generation
  { name: "wiki_compiler", displayName: "Wiki Compiler", description: "Compiles channel knowledge into wiki pages", group: "wiki" },
  { name: "wiki_maintainer", displayName: "Wiki Maintainer", description: "Keeps wiki pages fresh as new facts arrive", group: "wiki" },

  // QA / Ask
  { name: "qa_router", displayName: "QA Router", description: "Classifies ask-page questions and routes to deep vs. fast mode", group: "qa" },
  { name: "qa_agent", displayName: "QA Agent", description: "Answers user questions over channel knowledge with tool use", group: "qa" },

  // Utility / Other
  { name: "echo", displayName: "Echo (Debug)", description: "Pipeline validation agent for testing", group: "utility" },
  { name: "csv_mapper", displayName: "CSV Mapper", description: "Maps columns from imported CSV/JSONL files to message fields", group: "utility" },
];

export const GROUP_LABELS: Record<string, string> = {
  ingestion: "Ingestion Pipeline",
  media: "Media Processing",
  post_processing: "Post-Processing",
  wiki: "Wiki Generation",
  qa: "QA / Ask",
  utility: "Other",
};

/** Stable display order of the agent groups. */
export const GROUP_ORDER: AgentGroup[] = [
  "ingestion",
  "media",
  "post_processing",
  "wiki",
  "qa",
  "utility",
];

/** Lookup a meta record by consumer name (falls back to a synthesized entry). */
export function metaForConsumer(consumer: string): AgentMeta {
  const found = AGENT_META.find((a) => a.name === consumer);
  if (found) return found;
  return {
    name: consumer,
    displayName: consumer,
    description: "",
    group: "utility",
  };
}
