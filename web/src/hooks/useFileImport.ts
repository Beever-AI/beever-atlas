import { useCallback, useState } from "react";
import type {
  ImportCommitRequest,
  ImportCommitResponse,
  ImportPreviewResponse,
} from "@/lib/types";
import { authFetch } from "@/lib/api";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => ({}) as Record<string, unknown>);
  if (!res.ok) {
    const detail =
      typeof body?.detail === "string"
        ? (body.detail as string)
        : typeof body?.detail === "object"
          ? JSON.stringify(body.detail)
          : res.statusText;
    throw new Error(detail);
  }
  return body as T;
}

export interface UseFileImportReturn {
  preview: (file: File, useLlm: boolean) => Promise<ImportPreviewResponse>;
  commit: (body: ImportCommitRequest) => Promise<ImportCommitResponse>;
  previewing: boolean;
  committing: boolean;
  error: string | null;
}

export function useFileImport(): UseFileImportReturn {
  const [previewing, setPreviewing] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const preview = useCallback(
    async (file: File, useLlm: boolean): Promise<ImportPreviewResponse> => {
      setPreviewing(true);
      setError(null);
      try {
        const form = new FormData();
        form.append("file", file);
        form.append("use_llm", useLlm ? "true" : "false");
        // 60s timeout — LLM call can be slow; larger files still upload.
        const res = await authFetch(`${API_BASE}/api/imports/preview`, {
          method: "POST",
          body: form,
        });
        return await jsonOrThrow<ImportPreviewResponse>(res);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Preview failed";
        setError(msg);
        throw err;
      } finally {
        setPreviewing(false);
      }
    },
    [],
  );

  const commit = useCallback(
    async (body: ImportCommitRequest): Promise<ImportCommitResponse> => {
      setCommitting(true);
      setError(null);
      try {
        const res = await authFetch(`${API_BASE}/api/imports/commit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        return await jsonOrThrow<ImportCommitResponse>(res);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Commit failed";
        setError(msg);
        throw err;
      } finally {
        setCommitting(false);
      }
    },
    [],
  );

  return { preview, commit, previewing, committing, error };
}
