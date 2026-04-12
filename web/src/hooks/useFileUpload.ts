import { useState, useCallback } from "react";
import type { AttachmentFile } from "../types/askTypes";

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
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

    setUploading(true);
    setError(null);

    try {
      const formData = new FormData();
      formData.append("file", file);

      // Route to v2 endpoint when no channel (global Ask page), v1 otherwise
      const url = channelId
        ? `${API_BASE}/api/channels/${channelId}/ask/upload`
        : `${API_BASE}/api/ask/upload`;

      const res = await fetch(url, {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.error ?? `Upload failed (${res.status})`);
      }

      const attachment: AttachmentFile = await res.json();
      setFiles(prev => [...prev, attachment]);
      return attachment;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
      return null;
    } finally {
      setUploading(false);
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
