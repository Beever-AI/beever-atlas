import { useState, useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { RefreshCw, BookOpen, AlertTriangle, Sparkles, Network, FileText, Loader2, CheckCircle2, Circle, Database } from "lucide-react";
import { useWiki } from "@/hooks/useWiki";
import { useWikiPage } from "@/hooks/useWikiPage";
import { useWikiRefresh, type WikiGenerationStatus } from "@/hooks/useWikiRefresh";
import { useChannelMemoryCount } from "@/hooks/useChannelMemoryCount";
import { WikiLayout } from "@/components/wiki/WikiLayout";
import { OverviewPage } from "@/components/wiki/OverviewPage";
import { TopicPage } from "@/components/wiki/TopicPage";
import { GenericPage } from "@/components/wiki/GenericPage";
import { FaqPage } from "@/components/wiki/FaqPage";
import { Button } from "@/components/ui/button";
import type { WikiPage, WikiPageNode } from "@/lib/types";

function WikiLoadingSkeleton() {
  return (
    <div className="flex h-full">
      <div className="w-[220px] shrink-0 border-r border-slate-200 bg-white p-4 space-y-2">
        <div className="h-4 bg-slate-100 rounded animate-pulse w-3/4" />
        <div className="h-3 bg-slate-100 rounded animate-pulse w-1/2 mt-3" />
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="h-7 bg-slate-100 rounded animate-pulse" />
        ))}
      </div>
      <div className="flex-1 p-8 space-y-4">
        <div className="h-3 bg-slate-100 rounded animate-pulse w-1/4" />
        <div className="h-7 bg-slate-100 rounded animate-pulse w-1/2" />
        <div className="h-3 bg-slate-100 rounded animate-pulse w-1/6" />
        <div className="space-y-2 mt-6">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="h-4 bg-slate-100 rounded animate-pulse" style={{ width: `${70 + (i % 3) * 10}%` }} />
          ))}
        </div>
      </div>
    </div>
  );
}

const STAGE_LABELS: Record<string, string> = {
  starting: "Starting wiki generation",
  gathering: "Gathering memories, entities & topics",
  compiling: "Compiling pages with LLM",
  saving: "Saving wiki to cache",
  done: "Generation complete",
  error: "Generation failed",
};

const PAGE_LABELS: Record<string, string> = {
  overview: "Overview",
  people: "People & Experts",
  decisions: "Decisions",
  faq: "FAQ",
  glossary: "Glossary",
  activity: "Recent Activity",
  resources: "Resources & Media",
};

function getPageLabel(pageId: string): string {
  if (PAGE_LABELS[pageId]) return PAGE_LABELS[pageId];
  if (pageId.startsWith("topic-")) return pageId.replace("topic-", "").replace(/-/g, " ");
  return pageId;
}

function WikiGeneratingState({ status }: { status: WikiGenerationStatus }) {
  const stage = status.stage || "starting";
  const stageLabel = STAGE_LABELS[stage] || stage;
  const pagesTotal = status.pages_total || 0;
  const pagesDone = status.pages_done || 0;
  const pagesCompleted = status.pages_completed || [];
  const pagesRemaining = Math.max(0, pagesTotal - pagesDone);
  const progress = pagesTotal > 0 ? Math.round((pagesDone / pagesTotal) * 100) : 0;

  return (
    <div className="h-full min-h-0 bg-muted/10 px-6 py-8">
      <div className="mx-auto w-full max-w-lg rounded-2xl border border-border/70 bg-card/80 shadow-sm backdrop-blur-sm">
        <div className="px-6 py-8 sm:px-10 sm:py-10">
          <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl border border-primary/20 bg-primary/10">
            <Loader2 className="h-7 w-7 text-primary animate-spin" />
          </div>

          <h3 className="text-center text-xl font-semibold tracking-tight text-foreground">
            Generating Wiki
          </h3>
          <p className="mx-auto mt-1.5 text-center text-sm text-muted-foreground">
            {stageLabel}
          </p>

          {status.model && (
            <p className="mt-1 text-center text-xs text-muted-foreground/70">
              Model: {status.model}
            </p>
          )}

          {/* Progress bar for compiling stage */}
          {stage === "compiling" && pagesTotal > 0 && (
            <div className="mt-5">
              <div className="flex items-center justify-between text-xs text-muted-foreground mb-1.5">
                <span>{pagesDone} of {pagesTotal} pages compiled</span>
                <span>{progress}%</span>
              </div>
              <div className="h-2 rounded-full bg-muted overflow-hidden">
                <div
                  className="h-full rounded-full bg-primary transition-all duration-500 ease-out"
                  style={{ width: `${progress}%` }}
                />
              </div>
            </div>
          )}

          {/* Page checklist during compiling */}
          {stage === "compiling" && pagesTotal > 0 && (
            <div className="mt-4 space-y-1.5 max-h-52 overflow-y-auto">
              {/* Show fixed pages first, then topics */}
              {["overview", "people", "decisions", "faq", "glossary", "activity", "resources"].map((pageId) => {
                const done = pagesCompleted.includes(pageId);
                return (
                  <div key={pageId} className="flex items-center gap-2 text-xs">
                    {done ? (
                      <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500 shrink-0" />
                    ) : (
                      <Circle className="h-3.5 w-3.5 text-muted-foreground/40 shrink-0" />
                    )}
                    <span className={done ? "text-foreground" : "text-muted-foreground"}>
                      {getPageLabel(pageId)}
                    </span>
                  </div>
                );
              })}
              {/* Topic pages */}
              {pagesCompleted
                .filter((p) => p.startsWith("topic-"))
                .map((pageId) => (
                  <div key={pageId} className="flex items-center gap-2 text-xs">
                    <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500 shrink-0" />
                    <span className="text-foreground capitalize">
                      {getPageLabel(pageId)}
                    </span>
                  </div>
                ))}
              {/* Remaining page count */}
              {pagesRemaining > 0 && (
                <div className="flex items-center gap-2 text-xs">
                  <Loader2 className="h-3.5 w-3.5 text-muted-foreground/60 animate-spin shrink-0" />
                  <span className="text-muted-foreground">
                    {pagesRemaining} page{pagesRemaining !== 1 ? "s" : ""} remaining
                  </span>
                </div>
              )}
            </div>
          )}

          {/* Gathering stage indicator */}
          {stage === "gathering" && (
            <div className="mt-5 flex items-center justify-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              <span>Querying knowledge stores…</span>
            </div>
          )}

          {/* Saving stage indicator */}
          {stage === "saving" && (
            <div className="mt-5 flex items-center justify-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              <span>Writing pages to cache…</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function WikiRegeneratingBanner({ status }: { status: WikiGenerationStatus }) {
  const stage = status.stage || "starting";
  const stageLabel = STAGE_LABELS[stage] || stage;
  const stageDetail = status.stage_detail || "";
  const pagesTotal = status.pages_total || 0;
  const pagesDone = status.pages_done || 0;
  const pagesCompleted = status.pages_completed || [];
  const pagesRemaining = Math.max(0, pagesTotal - pagesDone);
  const progress = pagesTotal > 0 ? Math.round((pagesDone / pagesTotal) * 100) : 0;
  const completedTopics = pagesCompleted.filter((p) => p.startsWith("topic-"));
  const fixedOrder = ["overview", "people", "decisions", "faq", "glossary", "activity", "resources"];

  return (
    <div className="mb-5 rounded-2xl border border-border/70 bg-card/95 p-5 shadow-sm">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Loader2 className="h-5 w-5 text-primary animate-spin shrink-0" />
          <p className="text-base text-foreground truncate">
            Regenerating wiki: <span className="text-muted-foreground">{stageLabel}</span>
          </p>
        </div>
        {stage === "compiling" && pagesTotal > 0 && (
          <span className="text-sm text-muted-foreground shrink-0">
            {pagesDone}/{pagesTotal} ({progress}%)
          </span>
        )}
      </div>
      {stageDetail && (
        <p className="mt-2 text-sm text-muted-foreground">
          {stageDetail}
        </p>
      )}
      {status.model && (
        <p className="mt-1 text-sm text-muted-foreground/80">
          Model: {status.model}
        </p>
      )}
      {stage === "compiling" && pagesTotal > 0 && (
        <div className="mt-3">
          <div className="h-2 rounded-full bg-muted overflow-hidden">
            <div
              className="h-full rounded-full bg-primary transition-all duration-500 ease-out"
              style={{ width: `${progress}%` }}
            />
          </div>
          <div className="mt-3 space-y-1.5 max-h-52 overflow-y-auto pr-1">
            {fixedOrder.map((pageId) => {
              const done = pagesCompleted.includes(pageId);
              return (
                <div key={pageId} className="flex items-center gap-2 text-sm">
                  {done ? (
                    <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0" />
                  ) : (
                    <Circle className="h-4 w-4 text-muted-foreground/40 shrink-0" />
                  )}
                  <span className={done ? "text-foreground" : "text-muted-foreground"}>
                    {getPageLabel(pageId)}
                  </span>
                </div>
              );
            })}
            {completedTopics.map((pageId) => (
              <div key={pageId} className="flex items-center gap-2 text-sm">
                <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0" />
                <span className="text-foreground capitalize">{getPageLabel(pageId)}</span>
              </div>
            ))}
            {pagesRemaining > 0 && (
              <div className="flex items-center gap-2 text-sm">
                <Loader2 className="h-4 w-4 text-muted-foreground/60 animate-spin shrink-0" />
                <span className="text-muted-foreground">
                  {pagesRemaining} page{pagesRemaining !== 1 ? "s" : ""} remaining
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

interface WikiEmptyStateProps {
  onRefresh: () => void;
  onGoToMessages: () => void;
  onGoToSyncHistory: () => void;
  isRefreshing: boolean;
  hasError: boolean;
  isNoMemory: boolean;
  errorMessage?: string;
}

function WikiEmptyState({
  onRefresh,
  onGoToMessages,
  onGoToSyncHistory,
  isRefreshing,
  hasError,
  isNoMemory,
  errorMessage,
}: WikiEmptyStateProps) {
  const showGenerateCta = !hasError && !isNoMemory;

  return (
    <div className="h-full min-h-0 bg-muted/10 px-6 py-8">
      <div className="mx-auto w-full max-w-2xl rounded-2xl border border-border/70 bg-card/80 shadow-sm backdrop-blur-sm">
        <div className="px-6 py-8 text-center sm:px-10 sm:py-10">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-primary/20 bg-primary/10">
            {hasError ? (
              <AlertTriangle className="h-7 w-7 text-amber-500" />
            ) : isNoMemory ? (
              <Database className="h-7 w-7 text-primary" />
            ) : (
              <BookOpen className="h-7 w-7 text-primary" />
            )}
          </div>

          <h3 className="text-xl font-semibold tracking-tight text-foreground">
            {hasError
              ? "Could not load wiki"
              : isNoMemory
                ? "No channel knowledge yet"
                : "Wiki not generated yet"}
          </h3>
          <p className="mx-auto mt-2 max-w-xl text-sm text-muted-foreground">
            {hasError
              ? errorMessage || "The wiki is unavailable right now. Retry generation to rebuild this channel knowledge view."
              : isNoMemory
                ? "Sync this channel first to capture memories. Once data is available, the wiki can be generated from real channel knowledge."
                : "Generate a structured wiki to turn this channel history into topics, references, and easy-to-scan summaries."}
          </p>

          {showGenerateCta && (
            <div className="mx-auto mt-6 grid max-w-xl gap-2.5 text-left sm:grid-cols-3">
              <div className="rounded-xl border border-border/70 bg-muted/25 p-3">
                <Sparkles className="mb-2 h-4 w-4 text-primary" />
                <p className="text-xs font-medium text-foreground">Auto summaries</p>
                <p className="mt-1 text-xs text-muted-foreground">High-signal channel recap</p>
              </div>
              <div className="rounded-xl border border-border/70 bg-muted/25 p-3">
                <Network className="mb-2 h-4 w-4 text-primary" />
                <p className="text-xs font-medium text-foreground">Topic map</p>
                <p className="mt-1 text-xs text-muted-foreground">Related pages and relationships</p>
              </div>
              <div className="rounded-xl border border-border/70 bg-muted/25 p-3">
                <FileText className="mb-2 h-4 w-4 text-primary" />
                <p className="text-xs font-medium text-foreground">Reference pages</p>
                <p className="mt-1 text-xs text-muted-foreground">Context with source-backed detail</p>
              </div>
            </div>
          )}

          <div className="mt-7 flex justify-center gap-2">
            {showGenerateCta || hasError ? (
              <Button
                onClick={onRefresh}
                disabled={isRefreshing}
                size="lg"
                className="px-5"
              >
                <RefreshCw className={isRefreshing ? "animate-spin" : ""} />
                {isRefreshing
                  ? hasError
                    ? "Retrying..."
                    : "Generating..."
                  : hasError
                    ? "Retry Wiki Generation"
                    : "Generate Wiki"}
              </Button>
            ) : (
              <>
                <Button variant="outline" size="lg" className="px-5" onClick={onGoToMessages}>
                  Open Messages
                </Button>
                <Button variant="outline" size="lg" className="px-5" onClick={onGoToSyncHistory}>
                  View Sync History
                </Button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function renderPage(
  page: WikiPage,
  topicPages: WikiPageNode[],
  onNavigate: (pageId: string) => void,
) {
  if (page.id === "overview" || (page.page_type === "fixed" && page.slug === "overview")) {
    return <OverviewPage page={page} topicPages={topicPages} onNavigate={onNavigate} />;
  }
  if (page.page_type === "topic" || page.page_type === "sub-topic") {
    return <TopicPage page={page} onNavigate={onNavigate} />;
  }
  if (page.id === "faq" || page.slug === "faq") {
    return <FaqPage page={page} onNavigate={onNavigate} />;
  }
  return <GenericPage page={page} onNavigate={onNavigate} />;
}

export function WikiTab() {
  const { id: channelId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [activePageId, setActivePageId] = useState<string>("overview");

  const { data: wiki, isLoading, error, refetch } = useWiki(channelId);
  const { hasMemories, isLoading: isMemoryCountLoading } = useChannelMemoryCount(channelId);

  // Only fetch non-overview pages lazily
  const lazyPageId = activePageId !== "overview" ? activePageId : undefined;
  const { data: pageData, isLoading: isPageLoading } = useWikiPage(channelId, lazyPageId);

  const {
    mutate: triggerRefresh,
    isPending: isRefreshing,
    error: refreshError,
    generationStatus,
  } = useWikiRefresh(channelId);

  const handleRefresh = useCallback(() => {
    triggerRefresh(() => {
      // Called when generation is done — refetch the wiki
      refetch();
    });
  }, [triggerRefresh, refetch]);

  const handleNavigate = useCallback((pageId: string) => {
    setActivePageId(pageId);
  }, []);

  if (isLoading) {
    return <WikiLoadingSkeleton />;
  }

  if (!wiki && isMemoryCountLoading) {
    return <WikiLoadingSkeleton />;
  }

  // Show full-screen generation state only when wiki is not available yet.
  if (!wiki && isRefreshing && generationStatus && generationStatus.status === "running") {
    return <WikiGeneratingState status={generationStatus} />;
  }

  const isNoMemory = !isMemoryCountLoading && !hasMemories;

  if (error || !wiki) {
    return (
      <WikiEmptyState
        onRefresh={handleRefresh}
        onGoToMessages={() => navigate(`/channels/${channelId}/messages`)}
        onGoToSyncHistory={() => navigate(`/channels/${channelId}/sync-history`)}
        isRefreshing={isRefreshing}
        hasError={!!error || !!refreshError}
        isNoMemory={isNoMemory}
        errorMessage={refreshError?.message}
      />
    );
  }

  // Resolve the active page
  const activePage: WikiPage | null =
    activePageId === "overview" ? wiki.overview : (pageData ?? null);

  const topicPages = wiki.structure.pages.filter((p) => p.page_type === "topic");

  // Show a loading indicator inside the layout when fetching a non-overview page
  const pageContent =
    isPageLoading || !activePage ? (
      <div className="flex items-center justify-center py-16">
        <RefreshCw className="w-5 h-5 animate-spin text-slate-400" />
      </div>
    ) : (
      renderPage(activePage, topicPages, handleNavigate)
    );

  return (
    <WikiLayout
      channelId={channelId!}
      structure={wiki.structure}
      activePage={activePage ?? wiki.overview}
      onNavigate={handleNavigate}
      onRefresh={handleRefresh}
      isRefreshing={isRefreshing}
    >
      <>
        {isRefreshing && generationStatus && generationStatus.status === "running" && (
          <WikiRegeneratingBanner status={generationStatus} />
        )}
        {pageContent}
      </>
    </WikiLayout>
  );
}
