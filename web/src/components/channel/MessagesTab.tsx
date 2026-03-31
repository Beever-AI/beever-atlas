import { useState, useEffect, useRef, useCallback } from "react";
import { useParams, useOutletContext } from "react-router-dom";
import { api } from "@/lib/api";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertCircle, MessageSquare, ChevronDown, ChevronUp, ImageIcon, Play, FileText } from "lucide-react";
import { cn } from "@/lib/utils";
import { emojify } from "node-emoji";
import type { SyncState } from "@/hooks/useSync";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

interface Message {
  content: string;
  author: string;
  author_name: string;
  author_image: string;
  platform: string;
  channel_id: string;
  channel_name: string;
  message_id: string;
  timestamp: string;
  thread_id: string | null;
  attachments: Array<{ type: string; url?: string; name?: string }>;
  reactions: Array<{ name: string; count: number }>;
  reply_count: number;
  is_bot: boolean;
  subtype: string | null;
  links: Array<{ url: string; title?: string; description?: string; imageUrl?: string; siteName?: string }>;
}

interface ChannelSyncContext {
  syncState?: SyncState;
  isSyncing?: boolean;
}

// Deterministic color from string — light/dark variants paired
const AVATAR_COLORS = [
  "bg-primary/10 text-primary dark:bg-primary/15 dark:text-primary",
  "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300",
  "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300",
  "bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300",
  "bg-sky-100 text-sky-700 dark:bg-sky-950/50 dark:text-sky-300",
  "bg-violet-100 text-violet-700 dark:bg-violet-950/50 dark:text-violet-300",
];

function avatarColor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function initials(name: string): string {
  return name
    .split(/[\s._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0].toUpperCase())
    .join("");
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function reactionEmoji(name: string): string {
  const map: Record<string, string> = {
    thumbsup: "👍", thumbsdown: "👎", heart: "❤️", fire: "🔥",
    eyes: "👀", tada: "🎉", rocket: "🚀", check: "✅",
    "white_check_mark": "✅", plus1: "👍", minus1: "👎",
    laugh: "😄", joy: "😂", clap: "👏", thinking_face: "🤔",
  };
  return map[name] ?? `:${name}:`;
}

/**
 * Clean any residual Slack mrkdwn that survived bridge cleaning.
 * This is a frontend safety net — the bridge handles primary cleanup.
 */
function cleanResidualSlackMarkup(text: string): string {
  // <url|label> → label, <@U123> → @U123, <#C123|name> → #name
  return text.replace(/<([^>]+)>/g, (_m, inner: string) => {
    if (inner.startsWith("@")) {
      const parts = inner.split("|");
      return `@${parts[1] || inner.slice(1)}`;
    }
    if (inner.startsWith("#")) {
      const parts = inner.split("|");
      return `#${parts[1] || inner.slice(1)}`;
    }
    if (inner.startsWith("!")) {
      const parts = inner.split("|");
      if (parts[1]) return parts[1];
      return `@${inner.slice(1).split("^")[0]}`;
    }
    if (inner.includes("|")) return inner.split("|")[1];
    return inner;
  });
}

function renderContent(text: string): React.ReactNode {
  const cleaned = cleanResidualSlackMarkup(text);
  // Convert Slack emoji shortcodes (:tada:, :rocket:, etc.) to Unicode
  const withEmoji = emojify(cleaned, { fallback: (name) => `:${name}:` });
  // Split on URLs and @mentions (1-3 capitalized name words only)
  // Match @FirstName or @FirstName LastName (max 2 words for names)
  const parts = withEmoji.split(/(https?:\/\/[^\s]+|@[A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)/g);
  return parts.map((part, i) => {
    if (part.match(/^https?:\/\//)) {
      return <a key={i} href={part} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline break-all">{part}</a>;
    }
    if (part.match(/^@[A-Z]/)) {
      return <span key={i} className="bg-primary/10 text-primary rounded px-1 py-0.5 text-sm font-medium">{part}</span>;
    }
    return part;
  });
}

function ImageAttachment({ url, name }: { url: string; name: string }) {
  const [failed, setFailed] = useState(false);
  const [lightbox, setLightbox] = useState(false);

  useEffect(() => {
    if (!lightbox) return;
    const handleKey = (e: KeyboardEvent) => { if (e.key === "Escape") setLightbox(false); };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [lightbox]);

  if (failed) {
    return (
      <a href={url} target="_blank" rel="noopener noreferrer"
         className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-muted/40 border border-border text-sm text-foreground hover:bg-muted transition-colors">
        <ImageIcon size={14} className="text-muted-foreground shrink-0" />
        <span className="truncate">{name}</span>
      </a>
    );
  }

  return (
    <>
      <img
        src={url}
        alt={name}
        className="max-w-sm max-h-64 rounded-lg border border-border object-contain cursor-pointer hover:opacity-90 transition-opacity"
        onClick={() => setLightbox(true)}
        onError={() => setFailed(true)}
      />
      {lightbox && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
          onClick={() => setLightbox(false)}
        >
          <img
            src={url}
            alt={name}
            className="max-w-[90vw] max-h-[90vh] rounded-lg object-contain"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </>
  );
}

const MESSAGE_TRUNCATE_LENGTH = 500;

function MessageContent({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false);
  const isLong = content.length > MESSAGE_TRUNCATE_LENGTH;
  const displayText = isLong && !expanded ? content.slice(0, MESSAGE_TRUNCATE_LENGTH) + "…" : content;

  return (
    <div className="mt-0.5">
      <p className="text-[15px] sm:text-[16px] text-foreground/90 leading-relaxed whitespace-pre-wrap break-words">
        {renderContent(displayText)}
      </p>
      {isLong && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-sm font-medium text-primary hover:text-primary/80 mt-1"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

function MessageSkeleton() {
  return (
    <div className="flex gap-3 px-4 sm:px-6 py-3">
      <Skeleton className="w-8 h-8 rounded-full shrink-0" />
      <div className="flex-1 space-y-1.5">
        <Skeleton className="h-3 w-32" />
        <Skeleton className="h-4 w-full max-w-md" />
        <Skeleton className="h-4 w-3/4" />
      </div>
    </div>
  );
}

export function MessagesTab() {
  const { id: channelId } = useParams<{ id: string }>();
  const { syncState, isSyncing } = useOutletContext<ChannelSyncContext>();
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const wasSyncingRef = useRef(false);

  const fetchMessages = useCallback(() => {
    if (!channelId) return;
    setLoading(true);
    setError(null);
    api
      .get<Message[]>(`/api/channels/${channelId}/messages?limit=100`)
      .then(setMessages)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [channelId]);

  useEffect(() => {
    fetchMessages();
  }, [fetchMessages]);

  useEffect(() => {
    const currentlySyncing = isSyncing || syncState?.state === "syncing";
    if (wasSyncingRef.current && !currentlySyncing) {
      fetchMessages();
    }
    wasSyncingRef.current = currentlySyncing;
  }, [fetchMessages, isSyncing, syncState?.state, syncState?.job_id]);

  if (loading) {
    return (
      <div className="py-2 animate-fade-in">
        {Array.from({ length: 8 }).map((_, i) => (
          <MessageSkeleton key={i} />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6 animate-fade-in">
        <div className="flex items-start gap-2 bg-rose-50 dark:bg-rose-950/30 border border-rose-200 dark:border-rose-900 rounded-lg p-3">
          <AlertCircle size={14} className="text-rose-600 dark:text-rose-400 shrink-0 mt-0.5" />
          <p className="text-sm text-rose-700 dark:text-rose-400">{error}</p>
        </div>
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-center px-6 animate-fade-in">
        <MessageSquare size={24} className="text-muted-foreground/30 mb-3" />
        <p className="text-sm font-medium text-foreground">No messages yet</p>
        <p className="text-sm text-muted-foreground mt-1">
          Sync this channel to import its message history.
        </p>
      </div>
    );
  }

  // Filter out system messages (channel_join, channel_leave, etc.)
  const SYSTEM_SUBTYPES = new Set([
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive",
    "group_join", "group_leave", "bot_add", "bot_remove",
    "pinned_item", "unpinned_item",
  ]);
  // Content-based fallback: Chat SDK history may not expose subtype
  const SYSTEM_CONTENT_PATTERNS = /has joined the channel|has left the channel|set the channel topic|set the channel purpose|was added to the channel|was removed from the channel/i;
  const displayMessages = messages.filter((m) => {
    if (m.subtype && SYSTEM_SUBTYPES.has(m.subtype)) return false;
    if (SYSTEM_CONTENT_PATTERNS.test(m.content)) return false;
    return true;
  });

  const topLevel = displayMessages.filter((m) => !m.thread_id);

  return (
    <div className="animate-fade-in bg-muted/10 min-h-full">
      <div className="p-4 sm:p-6 py-6">
        <div className="max-w-4xl mx-auto space-y-5">
          <h2 className="text-base font-semibold tracking-tight text-foreground mb-1">
            {displayMessages.length} messages
          </h2>
          {topLevel.map((msg) => (
            <MessageThreadCard key={msg.message_id} msg={msg} channelId={channelId!} />
          ))}
        </div>
      </div>
    </div>
  );
}

function MessageThreadCard({ msg, channelId }: { msg: Message; channelId: string }) {
  const [expanded, setExpanded] = useState(false);
  const [replies, setReplies] = useState<Message[]>([]);
  const [loadingReplies, setLoadingReplies] = useState(false);
  const hasReplies = msg.reply_count > 0;

  const toggleReplies = useCallback(() => {
    if (!expanded && replies.length === 0 && hasReplies) {
      setLoadingReplies(true);
      api
        .get<Message[]>(`/api/channels/${channelId}/threads/${msg.message_id}/messages`)
        .then((data) => {
          // Filter out the parent message (first reply is the parent in Slack's API)
          setReplies(data.filter((m) => m.message_id !== msg.message_id));
        })
        .catch((err) => console.error("Failed to fetch thread replies:", err))
        .finally(() => setLoadingReplies(false));
    }
    setExpanded(!expanded);
  }, [expanded, replies.length, hasReplies, channelId, msg.message_id]);

  return (
    <div className="bg-card border border-border/60 shadow-sm rounded-xl p-5 transition-shadow hover:shadow-md">
      <MessageRow
        msg={msg}
        onToggleReplies={hasReplies ? toggleReplies : undefined}
        isExpanded={expanded}
      />
      {expanded && (
        <div className="relative mt-4 ml-[22px] pl-6 sm:pl-8 space-y-2 pt-2">
          <div className="absolute left-0 top-0 bottom-6 w-px bg-border/80" />
          {loadingReplies ? (
            <div className="py-3 space-y-2">
              {Array.from({ length: Math.min(msg.reply_count, 3) }).map((_, i) => (
                <div key={i} className="flex gap-3 px-2">
                  <Skeleton className="w-8 h-8 rounded-full shrink-0" />
                  <div className="flex-1 space-y-1.5">
                    <Skeleton className="h-3 w-24" />
                    <Skeleton className="h-4 w-full max-w-xs" />
                  </div>
                </div>
              ))}
            </div>
          ) : (
            replies.map((reply) => (
              <MessageRow key={reply.message_id} msg={reply} isReply />
            ))
          )}
        </div>
      )}
    </div>
  );
}

function MessageRow({ msg, isReply = false, onToggleReplies, isExpanded }: { msg: Message; isReply?: boolean; onToggleReplies?: () => void; isExpanded?: boolean }) {
  const displayName = msg.author_name || msg.author;
  const color = avatarColor(displayName);
  const inits = initials(displayName);

  return (
    <div className={cn("group relative flex gap-4 sm:gap-5 transition-colors", isReply && "hover:bg-muted/40 p-2.5 -mx-2.5 rounded-xl mt-1")}>
      {/* Avatar */}
      <Avatar className={cn("shrink-0", isReply ? "w-8 h-8 mt-0.5" : "w-11 h-11")}>
        {msg.author_image && <AvatarImage src={msg.author_image} alt={displayName} />}
        <AvatarFallback className={cn("font-medium", isReply ? "text-xs" : "text-[15px]", color)}>
          {inits}
        </AvatarFallback>
      </Avatar>

      {/* Content Container */}
      <div className="flex-1 min-w-0 flex flex-col pt-0.5">
        <div className="flex items-start justify-between gap-4 mb-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-[15px] font-bold text-foreground">
              {displayName}
            </span>
            {msg.is_bot && (
              <span className="px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide bg-muted text-muted-foreground border border-border">
                Bot
              </span>
            )}
            <span className="text-sm font-medium text-muted-foreground/80">
              {relativeTime(msg.timestamp)}
            </span>
          </div>

          {/* Reply badge aligned to far right */}
          {msg.reply_count > 0 && !isReply && (
            <button
              onClick={onToggleReplies}
              className="shrink-0 flex items-center gap-1.5 h-7 px-3 rounded-full bg-muted/80 border border-border/50 text-xs font-semibold text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
            >
              {msg.reply_count} {msg.reply_count === 1 ? "reply" : "replies"}
              {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </button>
          )}
        </div>

        <MessageContent content={msg.content} />

        {msg.links && msg.links.filter(l => l.title || l.description).length > 0 && (
          <div className="mt-2 space-y-2">
            {msg.links.filter(l => l.title || l.description).map((link, i) => (
              <a key={i} href={link.url} target="_blank" rel="noopener noreferrer"
                 className="block rounded-lg border border-border bg-muted/30 p-3 hover:bg-muted/50 transition-colors">
                <div className="flex gap-3">
                  {link.imageUrl && (
                    <img src={link.imageUrl} alt="" className="w-16 h-16 rounded object-cover shrink-0" />
                  )}
                  <div className="min-w-0 flex-1">
                    {link.siteName && <p className="text-xs text-muted-foreground">{link.siteName}</p>}
                    {link.title && <p className="text-sm font-medium text-foreground truncate">{link.title}</p>}
                    {link.description && <p className="text-xs text-muted-foreground line-clamp-2 mt-0.5">{link.description}</p>}
                  </div>
                </div>
              </a>
            ))}
          </div>
        )}

        {msg.attachments && msg.attachments.length > 0 && (
          <div className="mt-2 space-y-2">
            {msg.attachments.map((att, i) => {
              const isImage = att.type === "image" || /\.(png|jpe?g|gif|webp|svg)$/i.test(att.name || "");
              const isVideo = att.type === "video" || /\.(mp4|mov|webm|avi)$/i.test(att.name || "");
              const proxyUrl = att.url ? `${API_BASE}/api/files/proxy?url=${encodeURIComponent(att.url)}` : undefined;

              if (isImage && proxyUrl) {
                return <ImageAttachment key={i} url={proxyUrl} name={att.name || "Image"} />;
              }

              if (isVideo && proxyUrl) {
                return (
                  <a key={i} href={proxyUrl} target="_blank" rel="noopener noreferrer"
                     className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-muted/40 border border-border text-sm text-foreground hover:bg-muted transition-colors">
                    <Play size={14} className="text-muted-foreground shrink-0" />
                    <span className="truncate">{att.name || "Video"}</span>
                  </a>
                );
              }

              return (
                <a key={i} href={proxyUrl || "#"} target="_blank" rel="noopener noreferrer"
                   className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-muted/40 border border-border text-sm text-foreground hover:bg-muted transition-colors">
                  <FileText size={14} className="text-muted-foreground shrink-0" />
                  <span className="truncate">{att.name || "File"}</span>
                </a>
              );
            })}
          </div>
        )}

        {/* Reactions */}
        {msg.reactions.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2.5">
            {msg.reactions.map((r, i) => (
              <span
                key={i}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-muted/60 border border-border/50 text-xs hover:bg-muted transition-all cursor-default"
              >
                <span>{reactionEmoji(r.name)}</span>
                <span className="text-xs text-muted-foreground font-semibold">{r.count}</span>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
