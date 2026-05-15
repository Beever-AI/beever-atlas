/**
 * Fetch the wiki graph payload (Cytoscape format) for a channel.
 *
 * Always 200 — the backend endpoint returns ``{nodes:[], edges:[]}``
 * even when the graph backend is unavailable, so the hook treats a
 * non-OK response as a hard error rather than a soft "no graph".
 */
import { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";

export interface WikiGraphNode {
  data: {
    id: string;
    label?: string;
    kind?: "wiki" | "entity" | "channel";
    page_kind?: string;
    version?: number;
    last_updated?: string;
    entity_type?: string;
    // Mongo-fallback enrichments — present when nodes come from the
    // legacy ``wiki_cache`` document or the redesign ``wiki_pages``
    // store.
    page_id?: string;
    slug?: string;
    section_number?: string;
    summary?: string;
    memory_count?: number;
    legacy?: boolean;
  };
}

export interface WikiGraphEdge {
  data: {
    id: string;
    source: string;
    target: string;
    kind?: "references_wiki" | "references_entity" | "child_of" | "belongs_to";
    legacy?: boolean;
  };
}

export interface WikiGraphPayload {
  channel_id: string;
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
}

export function useWikiGraph(channelId: string | undefined) {
  const [data, setData] = useState<WikiGraphPayload | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchGraph = useCallback(async () => {
    if (!channelId) return;
    setIsLoading(true);
    setError(null);
    try {
      const payload = await api.get<WikiGraphPayload>(
        `/api/channels/${channelId}/wiki/graph`,
      );
      setData(payload);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to load wiki graph";
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, [channelId]);

  useEffect(() => {
    fetchGraph();
  }, [fetchGraph]);

  return { data, isLoading, error, refetch: fetchGraph };
}
