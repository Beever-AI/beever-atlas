import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, Link } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowLeft, ChevronRight, FileText, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";

interface EntityPageRow {
  page_id: string;
  title: string;
  slug: string;
  fact_count: number;
  updated_at: string | null;
}

interface EntityListResponse {
  channel_id: string;
  target_lang: string;
  count: number;
  pages: EntityPageRow[];
}

interface PageSection {
  id?: string;
  title?: string;
  content_md?: string;
}

interface PageDetail {
  page_id: string;
  title?: string;
  slug?: string;
  kind?: string;
  updated_at?: string;
  last_facts_seen?: string[];
  cross_links?: Record<string, unknown>;
  sections?: PageSection[];
}

function fmtRelativeTime(ts: string | null | undefined): string {
  if (!ts) return "—";
  try {
    const t = new Date(ts);
    const diffMs = Date.now() - t.getTime();
    const diffMin = Math.round(diffMs / 60_000);
    if (diffMin < 1) return "just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.round(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.round(diffHr / 24);
    return `${diffDay}d ago`;
  } catch {
    return ts;
  }
}

export function EntityPages() {
  const { channelId } = useParams<{ channelId: string }>();
  const [list, setList] = useState<EntityListResponse | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<PageDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  const fetchList = useCallback(async () => {
    if (!channelId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<EntityListResponse>(
        `/api/channels/${encodeURIComponent(channelId)}/wiki/entity-pages`,
      );
      setList(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load entity pages");
    } finally {
      setLoading(false);
    }
  }, [channelId]);

  const fetchDetail = useCallback(
    async (pageId: string) => {
      if (!channelId) return;
      setDetailLoading(true);
      try {
        const data = await api.get<PageDetail>(
          `/api/channels/${encodeURIComponent(channelId)}/wiki/pages/${encodeURIComponent(pageId)}`,
        );
        setDetail(data);
      } catch (err) {
        setDetail(null);
        setError(err instanceof Error ? err.message : "Failed to load page");
      } finally {
        setDetailLoading(false);
      }
    },
    [channelId],
  );

  useEffect(() => {
    void fetchList();
  }, [fetchList]);

  useEffect(() => {
    if (selected) void fetchDetail(selected);
    else setDetail(null);
  }, [selected, fetchDetail]);

  const filtered = useMemo(() => {
    if (!list) return [];
    const q = filter.trim().toLowerCase();
    if (!q) return list.pages;
    return list.pages.filter(
      (p) =>
        p.page_id.toLowerCase().includes(q) ||
        (p.title || "").toLowerCase().includes(q),
    );
  }, [list, filter]);

  const sortedByFacts = useMemo(
    () => [...filtered].sort((a, b) => b.fact_count - a.fact_count),
    [filtered],
  );

  if (!channelId) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Missing <code className="font-mono">channelId</code> in URL. Use{" "}
        <code className="font-mono">/admin/entity-pages/&lt;channelId&gt;</code>.
      </div>
    );
  }

  return (
    <div className="h-full overflow-hidden flex flex-col">
      <header className="border-b border-border px-6 py-3 flex items-center gap-3 shrink-0">
        <Link
          to={`/channels/${channelId}/wiki`}
          className="text-sm text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
        >
          <ArrowLeft size={14} /> Back to Channel Wiki
        </Link>
        <span className="text-muted-foreground">·</span>
        <h1 className="text-sm font-semibold text-foreground">
          Entity pages (debug)
        </h1>
        <span className="text-xs text-muted-foreground font-mono">
          {channelId}
        </span>
        <button
          type="button"
          onClick={() => void fetchList()}
          className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </header>

      {error && (
        <div className="m-4 rounded-lg border border-rose-200 dark:border-rose-900 bg-rose-50 dark:bg-rose-950/30 px-4 py-3 text-sm text-rose-700 dark:text-rose-300">
          {error}
        </div>
      )}

      <div className="flex-1 grid grid-cols-[minmax(280px,360px)_1fr] overflow-hidden">
        <aside className="border-r border-border overflow-auto">
          <div className="p-3 sticky top-0 bg-background border-b border-border">
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter by page id or title…"
              className="w-full px-3 py-1.5 rounded-md bg-muted/40 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <div className="mt-2 text-xs text-muted-foreground">
              {loading
                ? "Loading…"
                : list
                  ? `${sortedByFacts.length} of ${list.count} pages — sorted by fact count`
                  : "—"}
            </div>
          </div>
          <ul className="divide-y divide-border">
            {sortedByFacts.map((p) => {
              const active = p.page_id === selected;
              return (
                <li key={p.page_id}>
                  <button
                    type="button"
                    onClick={() => setSelected(p.page_id)}
                    className={
                      "w-full px-4 py-2.5 text-left flex items-start gap-2 transition-colors " +
                      (active
                        ? "bg-primary/10 text-foreground"
                        : "hover:bg-muted/40 text-foreground")
                    }
                  >
                    <FileText
                      size={14}
                      className="shrink-0 mt-0.5 text-muted-foreground"
                    />
                    <span className="flex-1 min-w-0">
                      <span className="block text-sm truncate">
                        {p.title || p.page_id}
                      </span>
                      <span className="block text-xs text-muted-foreground font-mono truncate">
                        {p.page_id}
                      </span>
                      <span className="block text-xs text-muted-foreground mt-0.5">
                        {p.fact_count} fact{p.fact_count === 1 ? "" : "s"} ·{" "}
                        {fmtRelativeTime(p.updated_at)}
                      </span>
                    </span>
                    {active && (
                      <ChevronRight
                        size={14}
                        className="shrink-0 mt-0.5 text-muted-foreground"
                      />
                    )}
                  </button>
                </li>
              );
            })}
            {!loading && sortedByFacts.length === 0 && (
              <li className="px-4 py-8 text-center text-sm text-muted-foreground">
                {list && list.count === 0
                  ? "No entity pages yet — wiki maintainer hasn't written any for this channel."
                  : "No matches for this filter."}
              </li>
            )}
          </ul>
        </aside>

        <main className="overflow-auto">
          {!selected && (
            <div className="h-full flex items-center justify-center text-sm text-muted-foreground">
              Pick an entity page on the left to view its full Markdown.
            </div>
          )}
          {selected && detailLoading && (
            <div className="p-6 text-sm text-muted-foreground">Loading page…</div>
          )}
          {selected && !detailLoading && detail && (
            <article className="p-6 max-w-3xl mx-auto">
              <header className="mb-4">
                <h2 className="text-2xl font-semibold text-foreground">
                  {detail.title || detail.page_id}
                </h2>
                <div className="mt-1 text-xs text-muted-foreground font-mono">
                  {detail.page_id}
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  <span>kind: {detail.kind || "—"}</span>
                  <span>·</span>
                  <span>
                    {(detail.last_facts_seen || []).length} fact{
                      (detail.last_facts_seen || []).length === 1 ? "" : "s"
                    }{" "}
                    referenced
                  </span>
                  <span>·</span>
                  <span>updated {fmtRelativeTime(detail.updated_at)}</span>
                </div>
              </header>
              {(detail.sections || []).length === 0 ? (
                <p className="text-sm text-muted-foreground italic">
                  This page has no content sections — likely a stub the
                  maintainer has not yet filled in.
                </p>
              ) : (
                (detail.sections || []).map((s, idx) => (
                  <section key={s.id || idx} className="mb-6">
                    {s.title && (
                      <h3 className="text-base font-semibold text-foreground mb-2">
                        {s.title}
                      </h3>
                    )}
                    <div className="prose prose-sm dark:prose-invert max-w-none">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {s.content_md || ""}
                      </ReactMarkdown>
                    </div>
                  </section>
                ))
              )}
            </article>
          )}
        </main>
      </div>
    </div>
  );
}

export default EntityPages;
