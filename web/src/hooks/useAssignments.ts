import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  Assignment,
  AssignmentListResponse,
  PresetResponse,
  UpdateAssignmentRequest,
} from "@/lib/aiSetup";

/**
 * React hook for managing per-consumer LLM assignments via the
 * ``/api/settings/assignments`` REST surface (PR-F).
 */
export function useAssignments() {
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [defaultConsumers, setDefaultConsumers] = useState<string[]>([]);
  const [capabilities, setCapabilities] = useState<Record<string, string[]>>({});
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.get<AssignmentListResponse>(
        "/api/settings/assignments"
      );
      setAssignments(data.assignments);
      setDefaultConsumers(data.default_consumers);
      setCapabilities(data.capabilities);
    } catch (err: any) {
      setError(err?.message || "Failed to load assignments");
    } finally {
      setIsLoading(false);
    }
  }, []);

  const upsert = useCallback(
    async (consumer: string, req: UpdateAssignmentRequest): Promise<Assignment> => {
      // PR-λ.6: always send ``force: true`` so the backend never 422s on a
      // capability mismatch. Atlas's substring-based capability classifier
      // is fundamentally a whack-a-mole (every new provider / new model
      // family is a missing pattern). Trying to gate operators by it locks
      // them out of legitimate models — and the runtime call itself is the
      // authoritative source of truth (visible via the "Last call"
      // indicator on the row). Operators who explicitly want the safety
      // gate can hit the API directly with ``force: false``.
      const saved = await api.put<Assignment>(
        `/api/settings/assignments/${consumer}`,
        { ...req, force: true }
      );
      await fetchAll();
      return saved;
    },
    [fetchAll]
  );

  const remove = useCallback(
    async (consumer: string): Promise<void> => {
      await api.delete(`/api/settings/assignments/${consumer}`);
      await fetchAll();
    },
    [fetchAll]
  );

  const previewPreset = useCallback(
    async (preset: string): Promise<PresetResponse> => {
      return api.post<PresetResponse>("/api/settings/assignments/preset", {
        preset,
        confirm: false,
      });
    },
    []
  );

  const applyPreset = useCallback(
    async (
      preset: string,
      force_overwrite_custom: boolean = false
    ): Promise<PresetResponse> => {
      const result = await api.post<PresetResponse>(
        "/api/settings/assignments/preset",
        { preset, confirm: true, force_overwrite_custom }
      );
      await fetchAll();
      return result;
    },
    [fetchAll]
  );

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  return {
    assignments,
    defaultConsumers,
    capabilities,
    isLoading,
    error,
    refetch: fetchAll,
    upsert,
    remove,
    previewPreset,
    applyPreset,
  };
}
