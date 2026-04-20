import { useState } from "react";
import { Hash } from "lucide-react";
import type { AttachmentFile, Message } from "@/types/askTypes";
import { useUserProfile } from "@/hooks/useUserProfile";
import { AttachmentPreviewModal } from "./AttachmentPreviewModal";

interface UserMessageProps {
  message: Message;
  /** Map of channel_id → display name, used to render the per-turn channel badge. */
  channelNames?: Record<string, string>;
}

function formatTime(date?: Date | string): string {
  if (!date) return "";
  const d = typeof date === "string" ? new Date(date) : date;
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  if (diff < 60000) return "Just now";
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
  return d.toLocaleDateString();
}

export function UserMessage({ message, channelNames }: UserMessageProps) {
  const { profile } = useUserProfile();
  const emoji = profile.avatarEmoji || "🦫";
  const avatarColor = profile.avatarColor || "hsl(215, 80%, 55%)";
  const [previewAttachment, setPreviewAttachment] = useState<AttachmentFile | null>(null);

  const channelId = message.channel_id;
  const channelLabel = channelId
    ? (channelNames?.[channelId] ?? channelId)
    : null;

  return (
    <>
    <div className="flex justify-end gap-3">
      <div className="max-w-[70%] min-w-0">
        <div className="bg-primary/10 rounded-2xl px-4 py-3 w-fit ml-auto">
          <p className="text-foreground text-sm whitespace-pre-wrap">{message.content}</p>
          {message.attachments && message.attachments.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2">
              {message.attachments.map((att) => (
                <button
                  key={att.file_id}
                  type="button"
                  onClick={() => setPreviewAttachment(att)}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 bg-muted rounded-md text-xs text-foreground/80 border border-border hover:bg-muted/70 transition-colors"
                  title={`Preview ${att.filename}`}
                >
                  📎 {att.filename}
                  {att.size_bytes ? (
                    <span className="text-muted-foreground/70">
                      ({(att.size_bytes / 1024).toFixed(0)}KB)
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="mt-1 flex items-center justify-end gap-2 text-[11px] text-muted-foreground/60">
          {channelLabel && (
            <>
              <span
                className="inline-flex items-center gap-1"
                title={`Asked in #${channelLabel}`}
              >
                <Hash className="size-2.5" />
                {channelLabel}
              </span>
              <span aria-hidden>·</span>
            </>
          )}
          <span>{formatTime(new Date())}</span>
        </div>
      </div>
      <div
        className="size-8 rounded-xl flex items-center justify-center text-white text-xs font-bold shrink-0"
        style={{ background: avatarColor }}
        title={profile.displayName || "You"}
      >
        {emoji}
      </div>
    </div>
    {previewAttachment && (
      <AttachmentPreviewModal
        attachment={previewAttachment}
        onClose={() => setPreviewAttachment(null)}
      />
    )}
    </>
  );
}
