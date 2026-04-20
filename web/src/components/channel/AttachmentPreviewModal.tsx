import { useEffect, useState } from "react";
import { X, Download, FileText, Loader2 } from "lucide-react";
import { API_BASE, authFetch } from "../../lib/api";
import type { AttachmentFile } from "../../types/askTypes";

interface AttachmentPreviewModalProps {
  attachment: AttachmentFile;
  onClose: () => void;
}

/** Preview / download modal for a saved Ask attachment.
 *
 * Fetches the blob via `authFetch` (so the Authorization header lands on
 * the GET) and renders images inline via `URL.createObjectURL`. Non-image
 * types surface a download button that triggers the same object URL.
 */
export function AttachmentPreviewModal({
  attachment,
  onClose,
}: AttachmentPreviewModalProps) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const isImage = attachment.mime_type?.startsWith("image/") ?? false;

  useEffect(() => {
    let cancelled = false;
    let createdUrl: string | null = null;
    const fetchBlob = async () => {
      setError(null);
      setObjectUrl(null);
      try {
        const res = await authFetch(
          `${API_BASE}/api/ask/files/${attachment.file_id}`,
        );
        if (!res.ok) {
          // Drain the body to keep the connection clean before erroring.
          await res.text().catch(() => "");
          throw new Error(
            res.status === 404
              ? "This attachment is no longer available."
              : res.status === 403
                ? "You don't have access to this attachment."
                : `Failed to load (${res.status})`,
          );
        }
        const blob = await res.blob();
        if (cancelled) return;
        createdUrl = URL.createObjectURL(blob);
        setObjectUrl(createdUrl);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load");
      }
    };
    fetchBlob();
    return () => {
      cancelled = true;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [attachment.file_id]);

  // Escape closes the modal — mirrors the MediaModal convention.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="relative w-full max-w-3xl max-h-[90vh] mx-4 rounded-xl border border-border bg-card shadow-2xl overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-border shrink-0">
          <div className="flex items-center gap-2.5 min-w-0">
            <FileText size={18} className="text-muted-foreground shrink-0" />
            <h3 className="font-semibold text-sm text-foreground truncate">
              {attachment.filename}
            </h3>
            {attachment.size_bytes ? (
              <span className="text-xs text-muted-foreground/70 shrink-0">
                {(attachment.size_bytes / 1024).toFixed(0)}KB
              </span>
            ) : null}
          </div>
          <div className="flex items-center gap-1">
            {objectUrl && (
              <a
                href={objectUrl}
                download={attachment.filename}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs rounded-lg hover:bg-muted transition-colors text-muted-foreground"
                title="Download"
              >
                <Download size={14} />
                <span className="hidden sm:inline">Download</span>
              </a>
            )}
            <button
              onClick={onClose}
              className="shrink-0 w-7 h-7 flex items-center justify-center rounded-lg hover:bg-muted transition-colors text-muted-foreground"
              aria-label="Close preview"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto p-5 flex items-center justify-center min-h-[200px]">
          {error ? (
            <p className="text-sm text-destructive text-center">{error}</p>
          ) : !objectUrl ? (
            <Loader2 className="w-6 h-6 animate-spin text-muted-foreground/60" />
          ) : isImage ? (
            <img
              src={objectUrl}
              alt={attachment.filename}
              className="max-w-full max-h-[75vh] object-contain rounded-lg bg-muted/20"
            />
          ) : (
            <div className="flex flex-col items-center gap-4 py-6">
              <FileText size={48} className="text-muted-foreground/50" />
              <p className="text-sm text-muted-foreground text-center max-w-md">
                Preview isn't supported for this file type — use the download
                button above to save it locally.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
