import { useState } from "react";
import { MoreHorizontal, Pin, Pencil, Trash2 } from "lucide-react";
import type { ConversationSession } from "../../hooks/useConversationHistory";

interface ConversationItemProps {
  session: ConversationSession;
  isActive: boolean;
  onSelect: () => void;
  onRename: (title: string) => void;
  onPin: () => void;
  onDelete: () => void;
}

function formatRelativeTime(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diff = now.getTime() - date.getTime();
  if (diff < 60_000) return "Just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h`;
  if (diff < 604_800_000) return `${Math.floor(diff / 86_400_000)}d`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function ConversationItem({
  session,
  isActive,
  onSelect,
  onRename,
  onPin,
  onDelete,
}: ConversationItemProps) {
  const [showMenu, setShowMenu] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState(
    session.title || session.first_question,
  );

  const displayTitle =
    session.title || session.first_question || "Untitled conversation";

  // Only show the preview when the title is custom/set, so we don't duplicate
  // the first_question content on both lines.
  const preview =
    session.title && session.title !== session.first_question
      ? session.first_question
      : null;

  const handleRename = () => {
    if (editTitle.trim()) {
      onRename(editTitle.trim());
      setEditing(false);
    }
  };

  return (
    <div
      className={`group relative flex items-start gap-2 pl-3 pr-2 py-2 rounded-lg cursor-pointer transition-colors duration-150 ${
        isActive
          ? "bg-primary/8 text-foreground"
          : "text-foreground/80 hover:bg-muted/50"
      }`}
      onClick={() => !editing && onSelect()}
    >
      {/* Active indicator rail */}
      <span
        aria-hidden
        className={`absolute left-0 top-2 bottom-2 w-[2px] rounded-r transition-all duration-200 ${
          isActive ? "bg-primary" : "bg-transparent"
        }`}
      />

      <div className="flex-1 min-w-0">
        {editing ? (
          <input
            type="text"
            value={editTitle}
            onChange={(e) => setEditTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleRename();
              if (e.key === "Escape") setEditing(false);
            }}
            onBlur={handleRename}
            autoFocus
            className="w-full text-[13px] bg-card border border-primary/40 rounded-md px-2 py-1 text-foreground outline-none focus:ring-2 focus:ring-primary/20"
            onClick={(e) => e.stopPropagation()}
          />
        ) : (
          <>
            <div className="flex items-center gap-1.5 min-w-0">
              {session.pinned && (
                <Pin
                  className="w-2.5 h-2.5 text-primary shrink-0"
                  fill="currentColor"
                  strokeWidth={0}
                />
              )}
              <p
                className={`text-[13px] leading-snug truncate ${
                  isActive ? "text-foreground font-medium" : "text-foreground/90"
                }`}
              >
                {displayTitle}
              </p>
            </div>
            {preview && (
              <p className="text-[11.5px] text-muted-foreground/70 truncate mt-0.5">
                {preview}
              </p>
            )}
            <p className="text-[11px] text-muted-foreground/50 mt-1">
              {formatRelativeTime(session.created_at)}
            </p>
          </>
        )}
      </div>

      {/* More menu trigger */}
      <div className="relative shrink-0 pt-0.5">
        <button
          onClick={(e) => {
            e.stopPropagation();
            setShowMenu(!showMenu);
          }}
          className={`p-1 rounded-md transition-all ${
            showMenu
              ? "opacity-100 bg-muted text-foreground"
              : "opacity-0 group-hover:opacity-100 hover:bg-muted text-muted-foreground"
          }`}
        >
          <MoreHorizontal className="w-3.5 h-3.5" />
        </button>

        {showMenu && (
          <>
            <div
              className="fixed inset-0 z-50"
              onClick={(e) => {
                e.stopPropagation();
                setShowMenu(false);
              }}
            />
            <div className="absolute right-0 top-7 bg-popover border border-border rounded-xl shadow-xl py-1 w-36 z-50 motion-safe:animate-scale-in origin-top-right">
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setEditing(true);
                  setShowMenu(false);
                }}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-foreground/80 hover:bg-muted transition-colors"
              >
                <Pencil className="w-3 h-3" /> Rename
              </button>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onPin();
                  setShowMenu(false);
                }}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-foreground/80 hover:bg-muted transition-colors"
              >
                <Pin className="w-3 h-3" /> {session.pinned ? "Unpin" : "Pin"}
              </button>
              <div className="my-1 border-t border-border/50" />
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete();
                  setShowMenu(false);
                }}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-destructive hover:bg-destructive/10 transition-colors"
              >
                <Trash2 className="w-3 h-3" /> Delete
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
