import { useState, useMemo, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Plus, Search, X, Pin, Loader2 } from "lucide-react";
import { useAskSessions } from "@/contexts/AskSessionsContext";
import { ConversationItem } from "@/components/channel/ConversationItem";
import type { GlobalConversationSession } from "@/hooks/useGlobalConversationHistory";

type Group = {
  label: string;
  items: GlobalConversationSession[];
};

function groupSessionsByTime(sessions: GlobalConversationSession[]): Group[] {
  const now = Date.now();
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);
  const sevenDaysAgo = new Date(now - 7 * 24 * 60 * 60 * 1000);

  const today: GlobalConversationSession[] = [];
  const yesterday: GlobalConversationSession[] = [];
  const thisWeek: GlobalConversationSession[] = [];
  const older: GlobalConversationSession[] = [];

  for (const s of sessions) {
    const d = new Date(s.created_at);
    if (d >= startOfToday) today.push(s);
    else if (d >= startOfYesterday) yesterday.push(s);
    else if (d >= sevenDaysAgo) thisWeek.push(s);
    else older.push(s);
  }

  const result: Group[] = [];
  if (today.length) result.push({ label: "Today", items: today });
  if (yesterday.length) result.push({ label: "Yesterday", items: yesterday });
  if (thisWeek.length) result.push({ label: "This Week", items: thisWeek });
  if (older.length) result.push({ label: "Older", items: older });
  return result;
}

export function SidebarConversationList() {
  const {
    sessions,
    activeSessionId,
    searchQuery,
    setSearchQuery,
    newConversation,
    renameSession,
    pinSession,
    deleteSession,
    hasMore,
    loadMore,
    loadingMore,
  } = useAskSessions();
  const navigate = useNavigate();

  const [searchFocused, setSearchFocused] = useState(false);
  const sentinelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && hasMore && !loadingMore) {
          loadMore();
        }
      },
      { threshold: 0.1 },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [hasMore, loadingMore, loadMore]);

  const pinnedSessions = useMemo(() => sessions.filter((s) => s.pinned), [sessions]);
  const unpinnedSessions = useMemo(() => sessions.filter((s) => !s.pinned), [sessions]);
  const timeGroups = useMemo(
    () => groupSessionsByTime(unpinnedSessions),
    [unpinnedSessions],
  );

  const handleSelectSession = (sessionId: string) => {
    // URL is canonical — route change writes activeSessionId via AskPage.
    navigate(`/ask/${sessionId}`);
  };

  const renderItem = (s: GlobalConversationSession) => (
    <ConversationItem
      key={s.session_id}
      session={s}
      isActive={s.session_id === activeSessionId}
      onSelect={() => handleSelectSession(s.session_id)}
      onRename={(title) => renameSession(s.session_id, title)}
      onPin={() => pinSession(s.session_id, !s.pinned)}
      onDelete={() => deleteSession(s.session_id)}
    />
  );

  return (
    <div className="flex flex-col h-full">
      {/* New chat button */}
      <div className="px-3 pt-3 pb-2">
        <button
          onClick={newConversation}
          className="w-full inline-flex items-center justify-center gap-2 h-8 px-3 text-xs font-medium text-foreground bg-background hover:bg-muted border border-border rounded-lg transition-colors"
          title="New conversation (⌘⇧O)"
        >
          <Plus className="w-3.5 h-3.5" />
          New chat
        </button>
      </div>

      {/* Search */}
      <div className="px-3 pb-2">
        <div
          className={`flex items-center gap-2 rounded-lg px-2.5 py-1.5 transition-all ${
            searchFocused
              ? "bg-background ring-1 ring-primary/30"
              : "bg-background/60"
          }`}
        >
          <Search className="w-3.5 h-3.5 text-muted-foreground/50 shrink-0" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onFocus={() => setSearchFocused(true)}
            onBlur={() => setSearchFocused(false)}
            placeholder="Search chats..."
            className="bg-transparent text-xs text-foreground placeholder-muted-foreground/40 outline-none w-full"
          />
          {searchQuery && (
            <button
              onClick={() => setSearchQuery("")}
              className="text-muted-foreground/50 hover:text-muted-foreground"
            >
              <X className="w-3 h-3" />
            </button>
          )}
        </div>
      </div>

      {/* Sessions */}
      <div className="flex-1 overflow-y-auto px-2 pb-3">
        {sessions.length === 0 ? (
          <p className="text-xs text-muted-foreground/50 text-center py-8">
            Your conversations will appear here
          </p>
        ) : (
          <>
            {pinnedSessions.length > 0 && (
              <section className="mb-3">
                <GroupHeader label="Pinned" icon={<Pin className="w-2.5 h-2.5" fill="currentColor" strokeWidth={0} />} />
                <div className="space-y-px mt-1">
                  {pinnedSessions.map(renderItem)}
                </div>
              </section>
            )}

            {timeGroups.map((group) => (
              <section key={group.label} className="mb-3 last:mb-0">
                <GroupHeader label={group.label} />
                <div className="space-y-px mt-1">
                  {group.items.map(renderItem)}
                </div>
              </section>
            ))}

            {/* Infinite scroll sentinel */}
            <div ref={sentinelRef} className="h-px" />
            {loadingMore && (
              <div className="flex justify-center py-2">
                <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground/40" />
              </div>
            )}
          </>
        )}
      </div>

      {/* Keyboard hint footer */}
      <div className="px-3 py-1.5 border-t border-border/30">
        <p className="text-[10px] text-muted-foreground/40 text-center">
          ⌘K toggle · ⌘⇧O new chat
        </p>
      </div>
    </div>
  );
}

function GroupHeader({
  label,
  icon,
}: {
  label: string;
  icon?: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-1.5 px-2 py-1">
      {icon && <span className="text-muted-foreground/50">{icon}</span>}
      <span className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/50">
        {label}
      </span>
    </div>
  );
}
