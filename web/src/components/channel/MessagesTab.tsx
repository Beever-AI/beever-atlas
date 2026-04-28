import { useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo } from "react";
import { createPortal } from "react-dom";
import { useParams, useOutletContext } from "react-router-dom";
import { api } from "@/lib/api";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Skeleton } from "@/components/ui/skeleton";
import {
  AlertCircle, MessageSquare, ChevronDown, ChevronUp,
  ImageIcon, Play, FileText, Search, ArrowUpDown, X,
  Paperclip, Loader2, RefreshCw, Calendar,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { emojify } from "node-emoji";
import type { SyncState } from "@/hooks/useSync";
import { buildLoaderUrl } from "@/lib/api";

const PAGE_SIZE = 50;
const POLL_INTERVAL_MS = 30_000;

interface Message {
  content: string;
  author: string;
  author_name: string;
  author_image: string | null;
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

interface MessagesPageResponse {
  messages: Message[];
  total_count: number | null;
}

interface ChannelSyncContext {
  syncState?: SyncState;
  isSyncing?: boolean;
  connectionId?: string | null;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

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

function fullTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", second: "2-digit",
  });
}

function dateLabel(iso: string): string {
  const d = new Date(iso);
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);

  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function dateKey(iso: string): string {
  return new Date(iso).toDateString();
}

function reactionEmoji(name: string): string {
  // Discord sends Unicode emoji directly (e.g. "👶") — return as-is
  if (/\p{Emoji_Presentation}/u.test(name)) return name;
  const result = emojify(`:${name}:`, { fallback: () => "" });
  if (result && result !== `:${name}:`) return result;
  const slackMap: Record<string, string> = {
    thumbsup: "👍", thumbsdown: "👎", plus1: "👍", minus1: "👎",
    "white_check_mark": "✅", check: "✅",
  };
  return slackMap[name] ?? `:${name}:`;
}

function cleanResidualSlackMarkup(text: string): string {
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
  const withEmoji = emojify(cleaned, { fallback: (name) => `:${name}:` });
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

// ── System message filter ────────────────────────────────────────────────────

const SYSTEM_SUBTYPES = new Set([
  "channel_join", "channel_leave", "channel_topic", "channel_purpose",
  "channel_name", "channel_archive", "channel_unarchive",
  "group_join", "group_leave", "bot_add", "bot_remove",
  "pinned_item", "unpinned_item",
]);
const SYSTEM_CONTENT_PATTERNS = /has joined the channel|has left the channel|set the channel topic|set the channel purpose|was added to the channel|was removed from the channel/i;

function isSystemMessage(m: Message): boolean {
  if (m.subtype && SYSTEM_SUBTYPES.has(m.subtype)) return true;
  if (SYSTEM_CONTENT_PATTERNS.test(m.content)) return true;
  return false;
}

// ── Sparkline ────────────────────────────────────────────────────────────────

function Sparkline({ messages }: { messages: Message[] }) {
  const dailyCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const m of messages) {
      const key = new Date(m.timestamp).toISOString().slice(0, 10);
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    const sorted = [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
    return sorted.map(([, count]) => count);
  }, [messages]);

  if (dailyCounts.length < 2) return null;

  const max = Math.max(...dailyCounts);
  const w = dailyCounts.length * 8;
  const h = 24;

  return (
    <svg width={w} height={h} className="shrink-0 opacity-60" aria-label="Message activity">
      {dailyCounts.map((count, i) => {
        const barH = max > 0 ? (count / max) * (h - 2) : 0;
        return (
          <rect
            key={i}
            x={i * 8}
            y={h - barH}
            width={5}
            height={barH}
            rx={1}
            className="fill-primary/50"
          />
        );
      })}
    </svg>
  );
}

// ── Date Separator ───────────────────────────────────────────────────────────

function DateSeparator({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3 py-2">
      <div className="flex-1 h-px bg-border" />
      <span className="text-xs font-medium text-muted-foreground px-2">{label}</span>
      <div className="flex-1 h-px bg-border" />
    </div>
  );
}

// ── Sub-components ───────────────────────────────────────────────────────────

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
      {lightbox && createPortal(
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
        </div>,
        document.body,
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

// ── Main Component ───────────────────────────────────────────────────────────

export function MessagesTab() {
  const { id: channelId } = useParams<{ id: string }>();
  const { syncState, isSyncing, connectionId } = useOutletContext<ChannelSyncContext>();

  // Core state
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [order, setOrder] = useState<"desc" | "asc">("desc");

  // Filters
  const [searchQuery, setSearchQuery] = useState("");
  const [authorFilter, setAuthorFilter] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [attachmentsOnly, setAttachmentsOnly] = useState(false);

  // Auto-refresh
  const [newMessageCount, setNewMessageCount] = useState(0);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Jump to date
  const [jumpDate, setJumpDate] = useState("");

  const wasSyncingRef = useRef(false);

  // Use ref for connectionId to avoid recreating fetchMessages on null→value transition
  const connectionIdRef = useRef(connectionId);
  useLayoutEffect(() => {
    connectionIdRef.current = connectionId;
  });

  // Track previous channelId to only clear messages on actual channel change
  const prevChannelIdRef = useRef(channelId);

  // ── Fetch messages ─────────────────────────────────────────────────────

  const fetchMessages = useCallback(async (opts?: { before?: string; since?: string; replace?: boolean }) => {
    if (!channelId) return;
    const isLoadMore = !!opts?.before;
    if (isLoadMore) setLoadingMore(true);
    else setLoading(true);
    setError(null);
    setErrorStatus(null);

    const params = new URLSearchParams({ limit: String(PAGE_SIZE), order });
    if (connectionIdRef.current) params.set("connection_id", connectionIdRef.current);
    if (opts?.before) params.set("before", opts.before);
    if (opts?.since) params.set("since", opts.since);

    try {
      const data = await api.get<MessagesPageResponse>(`/api/channels/${channelId}/messages?${params}`);
      const pageMessages = data.messages ?? [];
      setTotalCount(data.total_count ?? null);
      if (opts?.replace || (!isLoadMore && !opts?.since)) {
        setMessages(pageMessages);
      } else if (isLoadMore) {
        setMessages((prev) => {
          const ids = new Set(prev.map((m) => m.message_id));
          const newMsgs = pageMessages.filter((m) => !ids.has(m.message_id));
          return [...prev, ...newMsgs];
        });
      } else if (opts?.since) {
        // Polling: prepend new messages
        setMessages((prev) => {
          const ids = new Set(prev.map((m) => m.message_id));
          const newMsgs = pageMessages.filter((m) => !ids.has(m.message_id));
          if (newMsgs.length > 0) setNewMessageCount((c) => c + newMsgs.length);
          return [...newMsgs, ...prev];
        });
        return; // Don't update hasMore for poll fetches
      }
      setHasMore(pageMessages.length >= PAGE_SIZE);
    } catch (err: any) {
      if (!isLoadMore) {
        setError(err.message);
        setErrorStatus(err.status ?? null);
      }
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [channelId, order]);

  // Initial fetch — only clear messages when channelId actually changes
  useEffect(() => {
    if (prevChannelIdRef.current !== channelId) {
      setMessages([]);
      setHasMore(true);
      setTotalCount(null);
      setNewMessageCount(0);
      setError(null);
      setErrorStatus(null);
      prevChannelIdRef.current = channelId;
    }
    fetchMessages();
  }, [fetchMessages]);

  // Re-fetch after sync completes
  useEffect(() => {
    const currentlySyncing = isSyncing || syncState?.state === "syncing";
    if (wasSyncingRef.current && !currentlySyncing) {
      fetchMessages();
    }
    wasSyncingRef.current = currentlySyncing;
  }, [fetchMessages, isSyncing, syncState?.state, syncState?.job_id]);

  // ── Auto-refresh polling ───────────────────────────────────────────────

  // Stop polling on non-retryable errors (4xx, 501)
  const shouldPoll = !errorStatus || (errorStatus >= 500 && errorStatus !== 501);

  useEffect(() => {
    if (messages.length === 0 || !shouldPoll) return;

    const poll = () => {
      if (document.hidden) return; // Skip when tab not visible
      const newest = messages[0]; // desc order: first is newest
      if (newest) {
        fetchMessages({ since: newest.timestamp });
      }
    };

    pollRef.current = setInterval(poll, POLL_INTERVAL_MS);

    const handleVisibility = () => {
      if (!document.hidden && messages.length > 0) {
        poll(); // Poll immediately when tab becomes visible
      }
    };
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [messages, fetchMessages, shouldPoll]);

  // ── Load more ──────────────────────────────────────────────────────────

  const handleLoadMore = useCallback(() => {
    if (messages.length === 0 || loadingMore) return;
    const oldest = messages[messages.length - 1];
    fetchMessages({ before: oldest.message_id });
  }, [messages, loadingMore, fetchMessages]);

  // ── Jump to date ───────────────────────────────────────────────────────

  const handleJumpToDate = useCallback((date: string) => {
    if (!date) return;
    setJumpDate(date);
    setMessages([]);
    setHasMore(true);
    setNewMessageCount(0);
    setSearchQuery("");
    setAuthorFilter("");
    setDateFrom("");
    setDateTo("");
    setAttachmentsOnly(false);
    const sinceIso = new Date(date).toISOString();
    fetchMessages({ since: sinceIso, replace: true });
  }, [fetchMessages]);

  // ── Sort toggle ────────────────────────────────────────────────────────

  const toggleOrder = useCallback(() => {
    setOrder((prev) => prev === "desc" ? "asc" : "desc");
  }, []);

  // ── Filtering ──────────────────────────────────────────────────────────

  const uniqueAuthors = useMemo(() => {
    const authors = new Map<string, string>();
    for (const m of messages) {
      if (!authors.has(m.author)) {
        authors.set(m.author, m.author_name || m.author);
      }
    }
    return [...authors.entries()].sort(([, a], [, b]) => a.localeCompare(b));
  }, [messages]);

  const filteredMessages = useMemo(() => {
    let result = messages.filter((m) => !isSystemMessage(m));

    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter((m) => m.content.toLowerCase().includes(q));
    }
    if (authorFilter) {
      result = result.filter((m) => m.author === authorFilter);
    }
    if (dateFrom) {
      const from = new Date(dateFrom).getTime();
      result = result.filter((m) => new Date(m.timestamp).getTime() >= from);
    }
    if (dateTo) {
      const to = new Date(dateTo + "T23:59:59").getTime();
      result = result.filter((m) => new Date(m.timestamp).getTime() <= to);
    }
    if (attachmentsOnly) {
      result = result.filter((m) => m.attachments && m.attachments.length > 0);
    }

    return result;
  }, [messages, searchQuery, authorFilter, dateFrom, dateTo, attachmentsOnly]);

  const topLevel = useMemo(() => filteredMessages.filter((m) => !m.thread_id), [filteredMessages]);

  const hasActiveFilters = searchQuery || authorFilter || dateFrom || dateTo || attachmentsOnly;
  const displayedTotalCount = totalCount ?? syncState?.parent_messages ?? syncState?.total_messages ?? null;

  const clearFilters = () => {
    setSearchQuery("");
    setAuthorFilter("");
    setDateFrom("");
    setDateTo("");
    setAttachmentsOnly(false);
  };

  // ── Messages with date separators ──────────────────────────────────────

  const messagesWithSeparators = useMemo(() => {
    const items: Array<{ type: "separator"; label: string } | { type: "message"; msg: Message }> = [];
    let lastDateKey = "";

    for (const msg of topLevel) {
      const dk = dateKey(msg.timestamp);
      if (dk !== lastDateKey) {
        items.push({ type: "separator", label: dateLabel(msg.timestamp) });
        lastDateKey = dk;
      }
      items.push({ type: "message", msg });
    }

    return items;
  }, [topLevel]);

  // ── Render ─────────────────────────────────────────────────────────────

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
    // 501: platform doesn't support message history (Teams, Telegram)
    if (errorStatus === 501) {
      return (
        <div className="flex flex-col items-center justify-center h-64 text-center px-6 animate-fade-in">
          <MessageSquare size={24} className="text-muted-foreground/30 mb-3" />
          <p className="text-sm font-medium text-foreground">Message history not available</p>
          <p className="text-xs text-muted-foreground mt-1">
            This platform does not support browsing past messages. Messages sent while connected will appear in real time.
          </p>
        </div>
      );
    }
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

  return (
    <div className="animate-fade-in bg-muted/10 h-full overflow-auto">
      <div className="p-4 sm:p-6 py-6">
        <div className="max-w-4xl mx-auto space-y-4">

          {/* ── Header ─────────────────────────────────────── */}
          <div className="flex flex-wrap items-center gap-3 mb-2">
            <h2 className="text-base font-semibold tracking-tight text-foreground">
              {displayedTotalCount !== null
                ? `${messages.length} loaded / ${displayedTotalCount} total`
                : `${messages.length} messages loaded`}
            </h2>
            <Sparkline messages={messages} />
            <div className="flex-1" />
            <button
              onClick={toggleOrder}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-muted/60 border border-border/50 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
            >
              <ArrowUpDown size={13} />
              {order === "desc" ? "Newest first" : "Oldest first"}
            </button>
          </div>

          {/* ── New messages banner ────────────────────────── */}
          {newMessageCount > 0 && (
            <button
              onClick={() => {
                setNewMessageCount(0);
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
              className="w-full flex items-center justify-center gap-2 py-2 rounded-lg bg-primary/10 border border-primary/20 text-sm font-medium text-primary hover:bg-primary/15 transition-colors"
            >
              <RefreshCw size={14} />
              {newMessageCount} new message{newMessageCount > 1 ? "s" : ""}
            </button>
          )}

          {/* ── Filters ────────────────────────────────────── */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[200px]">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <input
                type="text"
                placeholder="Search messages..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full h-8 pl-9 pr-3 rounded-lg bg-muted/40 border border-border/50 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
              />
            </div>
            <select
              value={authorFilter}
              onChange={(e) => setAuthorFilter(e.target.value)}
              className="h-8 px-2 rounded-lg bg-muted/40 border border-border/50 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
            >
              <option value="">All authors</option>
              {uniqueAuthors.map(([id, name]) => (
                <option key={id} value={id}>{name}</option>
              ))}
            </select>
            <input
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              className="h-8 px-2 rounded-lg bg-muted/40 border border-border/50 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
              title="From date"
            />
            <input
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              className="h-8 px-2 rounded-lg bg-muted/40 border border-border/50 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
              title="To date"
            />
            <button
              onClick={() => setAttachmentsOnly(!attachmentsOnly)}
              className={cn(
                "inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border text-xs font-medium transition-colors",
                attachmentsOnly
                  ? "bg-primary/10 border-primary/30 text-primary"
                  : "bg-muted/40 border-border/50 text-muted-foreground hover:bg-muted hover:text-foreground"
              )}
            >
              <Paperclip size={13} />
              Attachments
            </button>

            {/* Jump to date */}
            <div className="relative">
              <Calendar size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none" />
              <input
                type="date"
                value={jumpDate}
                onChange={(e) => handleJumpToDate(e.target.value)}
                className="h-8 pl-8 pr-2 rounded-lg bg-muted/40 border border-border/50 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
                title="Jump to date"
              />
            </div>

            {hasActiveFilters && (
              <button
                onClick={clearFilters}
                className="inline-flex items-center gap-1 h-8 px-2 rounded-lg text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
              >
                <X size={13} />
                Clear
              </button>
            )}
          </div>

          {/* ── Filter result count ────────────────────────── */}
          {hasActiveFilters && (
            <p className="text-xs text-muted-foreground">
              Showing {topLevel.length} of {messages.filter((m) => !isSystemMessage(m)).length} messages
            </p>
          )}

          {/* ── Message list with date separators ──────────── */}
          <div className="space-y-5">
            {messagesWithSeparators.map((item, i) => {
              if (item.type === "separator") {
                return <DateSeparator key={`sep-${item.label}-${i}`} label={item.label} />;
              }
              return (
                <MessageThreadCard
                  key={item.msg.message_id}
                  msg={item.msg}
                  channelId={channelId!}
                  connectionId={connectionId}
                />
              );
            })}
          </div>

          {/* ── Load more ──────────────────────────────────── */}
          {hasMore && !hasActiveFilters && (
            <div className="flex justify-center pt-4">
              <button
                onClick={handleLoadMore}
                disabled={loadingMore}
                className="inline-flex items-center gap-2 px-5 py-2 rounded-lg bg-muted/60 border border-border/50 text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors disabled:opacity-50"
              >
                {loadingMore ? (
                  <>
                    <Loader2 size={14} className="animate-spin" />
                    Loading...
                  </>
                ) : (
                  "Load more messages"
                )}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Message Thread Card ──────────────────────────────────────────────────────

function MessageThreadCard({ msg, channelId, connectionId }: { msg: Message; channelId: string; connectionId?: string | null }) {
  const [expanded, setExpanded] = useState(false);
  const [replies, setReplies] = useState<Message[]>([]);
  const [loadingReplies, setLoadingReplies] = useState(false);
  const hasReplies = msg.reply_count > 0;

  const toggleReplies = useCallback(() => {
    if (!expanded && replies.length === 0 && hasReplies) {
      setLoadingReplies(true);
      const params = connectionId ? `?connection_id=${connectionId}` : "";
      api
        .get<Message[]>(`/api/channels/${channelId}/threads/${msg.message_id}/messages${params}`)
        .then((data) => {
          setReplies(data.filter((m) => m.message_id !== msg.message_id));
        })
        .catch((err) => console.error("Failed to fetch thread replies:", err))
        .finally(() => setLoadingReplies(false));
    }
    setExpanded(!expanded);
  }, [expanded, replies.length, hasReplies, channelId, msg.message_id, connectionId]);

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

// ── Message Row ──────────────────────────────────────────────────────────────

function MessageRow({ msg, isReply = false, onToggleReplies, isExpanded }: { msg: Message; isReply?: boolean; onToggleReplies?: () => void; isExpanded?: boolean }) {
  const displayName = msg.author_name || msg.author;
  const color = avatarColor(displayName);
  const inits = initials(displayName);

  return (
    <div className={cn("group relative flex gap-4 sm:gap-5 transition-colors", isReply && "hover:bg-muted/40 p-2.5 -mx-2.5 rounded-xl mt-1")}>
      <Avatar className={cn("shrink-0", isReply ? "w-8 h-8 mt-0.5" : "w-11 h-11")}>
        {msg.author_image && <AvatarImage src={msg.author_image} alt={displayName} />}
        <AvatarFallback className={cn("font-medium", isReply ? "text-xs" : "text-[15px]", color)}>
          {inits}
        </AvatarFallback>
      </Avatar>

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
            <span
              className="text-sm font-medium text-muted-foreground/80 cursor-default"
              title={fullTimestamp(msg.timestamp)}
            >
              {relativeTime(msg.timestamp)}
            </span>
          </div>

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
              const proxyUrl = att.url ? buildLoaderUrl(`/api/files/proxy?url=${encodeURIComponent(att.url)}`) : undefined;

              if (isImage && proxyUrl) {
                return <ImageAttachment key={proxyUrl} url={proxyUrl} name={att.name || "Image"} />;
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
