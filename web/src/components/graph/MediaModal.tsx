import { X, ExternalLink, FileText, Image as ImageIcon, Film, Link2 } from "lucide-react";
import { buildLoaderUrl } from "@/lib/api";

interface MediaModalProps {
  name: string;
  url: string;
  mediaType: string; // "pdf" | "image" | "link" | "file" | "video"
  onClose: () => void;
}

export function MediaModal({ name, url, mediaType, onClose }: MediaModalProps) {
  const proxyUrl = buildLoaderUrl(`/api/files/proxy?url=${encodeURIComponent(url)}`);
  const isSlackFile = url.includes("files.slack.com");
  const displayUrl = isSlackFile ? proxyUrl : url;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div
        className="relative w-full max-w-2xl mx-4 rounded-xl border border-border bg-card shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-border">
          <div className="flex items-center gap-2.5 min-w-0">
            {mediaType === "pdf" && <FileText size={18} className="text-red-400 shrink-0" />}
            {mediaType === "image" && <ImageIcon size={18} className="text-blue-400 shrink-0" />}
            {mediaType === "video" && <Film size={18} className="text-purple-400 shrink-0" />}
            {mediaType === "link" && <Link2 size={18} className="text-emerald-400 shrink-0" />}
            {!["pdf", "image", "video", "link"].includes(mediaType) && <FileText size={18} className="text-muted-foreground shrink-0" />}
            <h3 className="font-semibold text-sm text-foreground truncate">{name}</h3>
            <span className="px-2 py-0.5 text-xs rounded-full bg-muted text-muted-foreground shrink-0 capitalize">
              {mediaType}
            </span>
          </div>
          <button
            onClick={onClose}
            className="shrink-0 w-7 h-7 flex items-center justify-center rounded-lg hover:bg-muted transition-colors text-muted-foreground"
          >
            <X size={16} />
          </button>
        </div>

        {/* Content */}
        <div className="p-5">
          {mediaType === "image" && (
            <div className="flex flex-col items-center gap-4">
              <img
                src={displayUrl}
                alt={name}
                className="max-h-96 w-full object-contain rounded-lg border border-border bg-muted/20"
                onError={(e) => {
                  (e.target as HTMLImageElement).style.display = "none";
                  const fallback = document.getElementById("media-modal-fallback");
                  if (fallback) fallback.style.display = "block";
                }}
              />
              <p id="media-modal-fallback" className="text-sm text-muted-foreground hidden">
                Failed to load image preview.
              </p>
            </div>
          )}

          {mediaType === "pdf" && (
            <div className="flex flex-col items-center gap-4 py-6">
              <FileText size={48} className="text-red-400/60" />
              <p className="text-sm text-muted-foreground text-center">
                PDF document attached to this conversation.
              </p>
              <a
                href={displayUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                <ExternalLink size={14} />
                Open PDF
              </a>
            </div>
          )}

          {mediaType === "link" && (
            <div className="flex flex-col items-center gap-4 py-6">
              <Link2 size={48} className="text-emerald-400/60" />
              <p className="text-sm text-foreground font-mono break-all text-center px-4">
                {url}
              </p>
              <a
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                <ExternalLink size={14} />
                Open Link
              </a>
            </div>
          )}

          {mediaType === "video" && (
            <div className="flex flex-col items-center gap-4 py-6">
              <Film size={48} className="text-purple-400/60" />
              <p className="text-sm text-muted-foreground text-center">
                Video file attached to this conversation.
              </p>
              <a
                href={displayUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                <ExternalLink size={14} />
                Open Video
              </a>
            </div>
          )}

          {!["pdf", "image", "video", "link"].includes(mediaType) && (
            <div className="flex flex-col items-center gap-4 py-6">
              <FileText size={48} className="text-muted-foreground/40" />
              <a
                href={displayUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                <ExternalLink size={14} />
                Open File
              </a>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
