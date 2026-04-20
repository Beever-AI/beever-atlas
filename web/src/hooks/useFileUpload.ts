import { useState, useCallback } from "react";
import type { AttachmentFile } from "../types/askTypes";
import { authFetch } from "../lib/api";

const API_BASE = import.meta.env.VITE_API_URL ?? "";

const SUPPORTED_TYPES = new Set([
  "application/pdf",
  "image/png",
  "image/jpeg",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "text/plain",
  "text/csv",
]);

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB
const MAX_FILES = 5;

export function useFileUpload(channelId: string) {
  const [files, setFiles] = useState<AttachmentFile[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Derive `uploading` from the list so parallel uploads don't race the
  // single boolean setter (A resolves first → flips to false even while
  // B is still in flight).  Any pending chip keeps the flag true.
  const uploading = files.some((f) => f.uploading);

  const validateFile = useCallback((file: File): string | null => {
    if (!SUPPORTED_TYPES.has(file.type)) {
      return `Unsupported file type. Supported: PDF, PNG, JPG, DOCX, TXT, CSV`;
    }
    if (file.size > MAX_FILE_SIZE) {
      return `File too large. Maximum size: 10MB`;
    }
    if (files.length >= MAX_FILES) {
      return `Maximum ${MAX_FILES} files per message`;
    }
    return null;
  }, [files.length]);

  const uploadFile = useCallback(async (file: File): Promise<AttachmentFile | null> => {
    const validationError = validateFile(file);
    if (validationError) {
      setError(validationError);
      return null;
    }

    // Optimistic pending chip: appears the moment the user picks a file,
    // so vision-extraction latency (3–5s for images) doesn't leave the
    // composer feeling frozen.
    const pendingId = `pending-${crypto.randomUUID()}`;
    const pending: AttachmentFile = {
      file_id: pendingId,
      filename: file.name,
      extracted_text: "",
      mime_type: file.type,
      size_bytes: file.size,
      uploading: true,
    };
    setFiles((prev) => [...prev, pending]);
    setError(null);

    try {
      const formData = new FormData();
      formData.append("file", file);

      // Route to v2 endpoint when no channel (global Ask page), v1 otherwise
      const url = channelId
        ? `${API_BASE}/api/channels/${channelId}/ask/upload`
        : `${API_BASE}/api/ask/upload`;

      const res = await authFetch(url, {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.error ?? `Upload failed (${res.status})`);
      }

      const attachment: AttachmentFile = await res.json();
      // Swap the pending entry for the resolved one in place so ordering
      // matches what the user selected.
      setFiles((prev) =>
        prev.map((f) => (f.file_id === pendingId ? attachment : f)),
      );
      return attachment;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
      // Drop the pending entry so a failed upload doesn't leave a stuck
      // spinner in the composer.
      setFiles((prev) => prev.filter((f) => f.file_id !== pendingId));
      return null;
    }
  }, [channelId, validateFile]);

  const removeFile = useCallback((fileId: string) => {
    setFiles(prev => prev.filter(f => f.file_id !== fileId));
  }, []);

  const clearFiles = useCallback(() => {
    setFiles([]);
    setError(null);
  }, []);

  return {
    files,
    uploading,
    error,
    uploadFile,
    removeFile,
    clearFiles,
    validateFile,
  };
}
