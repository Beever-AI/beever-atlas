import { useState, useCallback, useEffect, useMemo, Suspense, lazy, type ReactNode } from "react";
import { useLocation, useNavigate, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { wikiT } from "@/lib/wikiI18n";
import { buildWikiPath, preserveQueryParams } from "@/lib/wikiNav";
import { RefreshCw, BookOpen, AlertTriangle, Sparkles, Network, FileText, Loader2, CheckCircle2, Circle, ArrowRight, FolderSync, History as HistoryIcon, Download } from "lucide-react";

// Lazy-load the wiki graph so cytoscape (~200 KB) stays out of the
// wiki tab's main bundle until the operator toggles ?view=graph.
// Mirrors the pattern previously used in App.tsx (§6.6 + §6.13 —
// bundle-weight contract).
const WikiGraph = lazy(() => import("@/components/wiki/WikiGraph"));
import { PipelineEmptyState } from "@/components/shared/PipelineEmptyState";
import { FullscreenWrapper } from "@/components/shared/FullscreenWrapper";
import { useWiki } from "@/hooks/useWiki";
import { useExtractionStatus } from "@/hooks/useExtractionStatus";
import { useWikiPage } from "@/hooks/useWikiPage";
import { useWikiRefresh, type WikiGenerationStatus } from "@/hooks/useWikiRefresh";
import { useWikiVersions } from "@/hooks/useWikiVersions";
import { useWikiVersion } from "@/hooks/useWikiVersion";
import { useChannelMemoryCount } from "@/hooks/useChannelMemoryCount";
import { useRegenerateOverview } from "@/hooks/useRegenerateOverview";
import { WikiLayout } from "@/components/wiki/WikiLayout";
import { WikiHealthToolbar } from "@/components/wiki/WikiHealthToolbar";
import { SegmentedToggle } from "@/components/shared/SegmentedToggle";
import {
  ViewExplainerButton,
  type ExplainerSection,
} from "@/components/shared/ViewExplainerButton";
import { OverviewPage } from "@/components/wiki/OverviewPage";
import { FolderPage } from "@/components/wiki/FolderPage";
import { TopicPage } from "@/components/wiki/TopicPage";
import { GenericPage } from "@/components/wiki/GenericPage";
import { FaqPage } from "@/components/wiki/FaqPage";
import { WikiRegenerateButton } from "@/components/channel/WikiRegenerateButton";
import { CurationDropdown } from "@/components/wiki/CurationControls";
import { Button } from "@/components/ui/button";
import { api, authFetch, API_BASE } from "@/lib/api";
import type { WikiPage, WikiPageNode } from "@/lib/types";
import type { SyncState } from "@/hooks/useSync";

interface LanguageConfig {
  supported_languages: string[];
  default_target_language: string;
}

type WikiView = "pages" | "graph";

const WIKI_VIEW_OPTIONS = [
  { value: "pages" as const, label: "Wiki", icon: BookOpen, testId: "wiki-view-toggle-pages" },
  { value: "graph" as const, label: "WikiGraph", icon: Network, testId: "wiki-view-toggle-graph" },
];

// Plain-English explanations surfaced via the ViewExplainerButton next
// to the Wiki/WikiGraph toggle. Same pattern as the Memory tab so
// operators get consistent help across surfaces.
const WIKI_EXPLAINER_SECTIONS: ExplainerSection[] = [
  {
    title: "Wiki",
    icon: BookOpen,
    accent: "bg-primary/15 text-primary",
    tagline: "Auto-generated, structured documentation of your channel.",
    body: (
      <>
        <p>
          Beever Atlas distills every message into a living wiki:
          Overview, Topics, FAQ, Glossary, Decisions, and more — each
          page synthesized by an LLM from the underlying memories so it
          stays current as new conversations happen.
        </p>
        <p>
          Pages cite their sources with{" "}
          <code className="rounded bg-muted px-1 py-0.5 text-[12px]">
            [[wikilinks]]
          </code>{" "}
          to other pages and footnote-style citations back to the
          original messages. Use the search box to grep this page, the
          tools menu to maintain or regenerate it.
        </p>
      </>
    ),
  },
  {
    title: "WikiGraph",
    icon: Network,
    accent: "bg-emerald-500/15 text-emerald-500",
    tagline: "Visual map of how the wiki pages link together.",
    body: (
      <>
        <p>
          Every wiki page becomes a node, every cross-reference becomes
          an edge. Pages cluster by kind (Topic, Decision, FAQ, Action
          Item) so you can see the channel's structure at a glance and
          spot orphan pages or dense topic islands.
        </p>
        <p>
          Hover a node to highlight its neighborhood, click to preview
          the page in a side panel, or double-click to jump straight
          there.
        </p>
      </>
    ),
  },
];

function WikiLoadingSkeleton() {
  return (
    <div className="flex h-full">
      <div className="w-[220px] shrink-0 border-r border-border bg-card p-4 space-y-2">
        <div className="h-4 bg-muted rounded animate-pulse w-3/4" />
        <div className="h-3 bg-muted rounded animate-pulse w-1/2 mt-3" />
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="h-7 bg-muted rounded animate-pulse" />
        ))}
      </div>
      <div className="flex-1 p-8 space-y-4">
        <div className="h-3 bg-muted rounded animate-pulse w-1/4" />
        <div className="h-7 bg-muted rounded animate-pulse w-1/2" />
        <div className="h-3 bg-muted rounded animate-pulse w-1/6" />
        <div className="space-y-2 mt-6">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="h-4 bg-muted rounded animate-pulse" style={{ width: `${70 + (i % 3) * 10}%` }} />
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
    <div className="flex h-full min-h-0 items-center justify-center px-6 py-12">
      <div className="mx-auto w-full max-w-md text-center">
        {/* Icon */}
        <div className="mx-auto mb-5 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
          {hasError ? (
            <AlertTriangle className="h-5 w-5 text-amber-500" />
          ) : isNoMemory ? (
            <FolderSync className="h-5 w-5 text-muted-foreground" />
          ) : (
            <BookOpen className="h-5 w-5 text-muted-foreground" />
          )}
        </div>

        {/* Heading */}
        <h3 className="text-base font-semibold text-foreground">
          {hasError
            ? "Could not load wiki"
            : isNoMemory
              ? "Sync to unlock the wiki"
              : "Ready to generate"}
        </h3>

        {/* Body */}
        <p className="mx-auto mt-1.5 max-w-xs text-sm text-muted-foreground">
          {hasError
            ? errorMessage || "Something went wrong. Retry to rebuild this channel's wiki."
            : isNoMemory
              ? "Sync this channel to start building its wiki from real conversations."
              : "Turn this channel's history into topics, summaries, and references."}
        </p>

        {/* Two-step flow for no-memory state */}
        {isNoMemory && (
          <div className="mx-auto mt-6 flex max-w-xs items-center justify-center gap-3">
            <div className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2">
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-primary text-[10px] font-bold text-primary-foreground">1</span>
              <span className="text-xs font-medium text-foreground">Sync channel</span>
            </div>
            <ArrowRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground/50" />
            <div className="flex items-center gap-2 rounded-lg border border-border/50 bg-card/50 px-3 py-2">
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-muted text-[10px] font-bold text-muted-foreground">2</span>
              <span className="text-xs font-medium text-muted-foreground">Generate wiki</span>
            </div>
          </div>
        )}

        {/* Feature pills for generate CTA */}
        {showGenerateCta && (
          <div className="mx-auto mt-5 flex flex-wrap items-center justify-center gap-2">
            {[
              { icon: Sparkles, label: "Summaries" },
              { icon: Network, label: "Topic map" },
              { icon: FileText, label: "References" },
            ].map(({ icon: Icon, label }) => (
              <span
                key={label}
                className="inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-muted/30 px-2.5 py-1 text-xs text-muted-foreground"
              >
                <Icon className="h-3 w-3" />
                {label}
              </span>
            ))}
          </div>
        )}

        {/* Actions */}
        <div className="mt-6 flex flex-col items-center gap-2">
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
                  ? "Retry Generation"
                  : "Generate Wiki"}
            </Button>
          ) : (
            <>
              <Button variant="default" size="lg" className="px-5" onClick={onGoToMessages}>
                Open Messages
              </Button>
              <button
                onClick={onGoToSyncHistory}
                className="text-xs text-muted-foreground hover:text-foreground transition-colors"
              >
                View sync history
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** Format an elapsed-seconds count as a compact human-readable string.
 *  Under 60s → "Xs"; under an hour → "Xm Ys"; otherwise → "Xh Ym".
 *  Used by the overview-wiki in-flight screen to show "Elapsed: 3m 42s"
 *  ticking live so the user knows the build is still alive and roughly
 *  when they can reasonably expect to retry. */
function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return `${m}m ${rem}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

interface OverviewInFlightStateProps {
  channelId: string | undefined;
  description: string;
  startedAt?: string;
}

/** In-flight loading screen for the auto-overview build with a live
 *  elapsed-time counter and graduated retry affordances:
 *    * elapsed > 3 min → subdued "Taking longer than usual. Retry?" link
 *    * elapsed > 10 min → prominent "Retry overview generation" button
 *
 *  Falls back gracefully when ``startedAt`` is missing (legacy
 *  backends) — renders the description without a number and surfaces
 *  the Retry button immediately so the user is never trapped. */
function OverviewInFlightState({
  channelId,
  description,
  startedAt,
}: OverviewInFlightStateProps) {
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const startedMs = useMemo(() => {
    if (!startedAt) return null;
    const t = Date.parse(startedAt);
    return Number.isFinite(t) ? t : null;
  }, [startedAt]);

  const elapsedSeconds =
    startedMs !== null ? Math.max(0, Math.round((nowMs - startedMs) / 1000)) : null;

  const showSubduedRetry = elapsedSeconds !== null && elapsedSeconds > 180;
  const showProminentRetry =
    elapsedSeconds === null || elapsedSeconds > 600;

  const {
    isPending: isRetryPending,
    error: retryError,
    succeeded: retrySucceeded,
    regenerate,
  } = useRegenerateOverview(channelId);

  const onRetry = useCallback(() => {
    void regenerate();
  }, [regenerate]);

  return (
    <PipelineEmptyState
      icon={BookOpen}
      title="Generating overview wiki…"
      description={
        elapsedSeconds !== null
          ? `${description} Elapsed: ${formatElapsed(elapsedSeconds)}.`
          : `${description} The build has been running for a while.`
      }
      steps={[
        { label: "Sync channel", icon: FolderSync, done: true, active: false },
        { label: "Build memories", icon: Sparkles, done: true, active: false },
        { label: "Generate wiki", icon: BookOpen, done: false, active: true },
      ]}
    >
      {retrySucceeded && (
        <p className="text-xs text-emerald-600 dark:text-emerald-500">
          Restarted — give it a few moments.
        </p>
      )}
      {retryError && (
        <p className="text-xs text-red-600 dark:text-red-500">
          {retryError.message}
        </p>
      )}
      {showProminentRetry ? (
        <Button
          size="lg"
          onClick={onRetry}
          disabled={isRetryPending}
          className="px-5"
        >
          <RefreshCw className={isRetryPending ? "animate-spin" : ""} />
          {isRetryPending ? "Restarting…" : "Retry overview generation"}
        </Button>
      ) : showSubduedRetry ? (
        <button
          type="button"
          onClick={onRetry}
          disabled={isRetryPending}
          className="text-xs text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
        >
          {isRetryPending ? "Restarting…" : "Taking longer than usual. Retry?"}
        </button>
      ) : null}
    </PipelineEmptyState>
  );
}

function renderPage(
  page: WikiPage,
  topicPages: WikiPageNode[],
  onNavigate: (pageId: string) => void,
  lang?: string,
  folderPages: WikiPageNode[] = [],
  generatedAt?: string,
) {
  if (page.id === "overview" || (page.page_type === "fixed" && page.slug === "overview")) {
    return (
      <OverviewPage
        page={page}
        topicPages={topicPages}
        folderPages={folderPages}
        generatedAt={generatedAt}
        onNavigate={onNavigate}
        lang={lang}
      />
    );
  }
  if (page.page_type === "folder") {
    return <FolderPage page={page} onNavigate={onNavigate} lang={lang} />;
  }
  if (page.page_type === "topic" || page.page_type === "sub-topic") {
    return <TopicPage page={page} onNavigate={onNavigate} lang={lang} />;
  }
  if (page.id === "faq" || page.slug === "faq") {
    return <FaqPage page={page} onNavigate={onNavigate} lang={lang} />;
  }
  return <GenericPage page={page} onNavigate={onNavigate} lang={lang} />;
}

export function WikiTab() {
  const { id: channelId, slug: routeSlug } = useParams<{ id: string; slug?: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  // Null-safe outlet destructure — ``useOutletContext`` returns ``null``
  // when the component is rendered outside an ``<Outlet context={...}>``
  // (e.g., the vitest harness in WikiTab.test.tsx). Without the ``?? {}``
  // fallback, a plain destructure crashes with "Cannot destructure
  // property 'triggerSync' of ... as it is null". Same defensive pattern
  // TierBrowser.tsx uses.
  const { triggerSync, isSyncing, syncState } = useOutletContext<{
    triggerSync?: () => Promise<void>;
    isSyncing?: boolean;
    syncState?: SyncState;
  }>() ?? {};
  // ``searchParams`` still drives view/lang/version state — only the
  // legacy ``?page=`` query is dead. ``setSearchParams`` is used for
  // those non-page params; page navigation goes through ``navigate``
  // with ``buildWikiPath``.
  const [searchParams, setSearchParams] = useSearchParams();
  // ``?view=graph`` keeps the wiki cross-link graph mounted in-tab so
  // a back-button or refresh restores what the operator was looking
  // at. The toggle writes via ``replace: true`` so flipping doesn't
  // pollute history.
  const view: WikiView = searchParams.get("view") === "graph" ? "graph" : "pages";
  const setView = useCallback((next: WikiView) => {
    const updated = new URLSearchParams(searchParams);
    if (next === "pages") {
      updated.delete("view");
    } else {
      updated.set("view", next);
    }
    setSearchParams(updated, { replace: true });
  }, [searchParams, setSearchParams]);
  // ``viewingVersionNumber`` is URL-driven via ``?version=N`` so a
  // browser refresh, a shared link, or a navigation in-place all
  // restore the historical-version view. Local state would silently
  // drop the version on reload — the user reported this regression.
  const versionParam = searchParams.get("version");
  const viewingVersionNumber: number | null = useMemo(() => {
    if (!versionParam) return null;
    const n = parseInt(versionParam, 10);
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [versionParam]);
  const setViewingVersionNumber = useCallback(
    (next: number | null) => {
      const updated = new URLSearchParams(searchParams);
      if (next === null || next <= 0) {
        updated.delete("version");
      } else {
        updated.set("version", String(next));
      }
      setSearchParams(updated, { replace: true });
    },
    [searchParams, setSearchParams],
  );
  const [versionHistoryOpen, setVersionHistoryOpen] = useState(false);

  const [langConfig, setLangConfig] = useState<LanguageConfig | null>(null);
  const [targetLang, setTargetLang] = useState<string>(() => {
    if (!channelId) return "en";
    return localStorage.getItem(`wiki.targetLang.${channelId}`) ?? "en";
  });

  // Fetch language config and channel primary_language once on mount
  useEffect(() => {
    api.get<LanguageConfig>("/api/config/languages").then((cfg) => {
      setLangConfig(cfg);
      // Hydrate targetLang from localStorage, fallback to default
      if (channelId) {
        const stored = localStorage.getItem(`wiki.targetLang.${channelId}`);
        setTargetLang(stored ?? cfg.default_target_language);
      }
    }).catch(() => {});

  }, [channelId]);

  const { data: wiki, isLoading, error, isNotFound, refetch } = useWiki(channelId, targetLang);
  const { hasMemories, isLoading: isMemoryCountLoading, refetch: refetchMemoryCount } = useChannelMemoryCount(channelId);

  // ── Slug ↔ pageId maps ─────────────────────────────────────────────
  // Path-based routing means the URL carries a SLUG (human-readable,
  // SEO-friendly), but every component below works with PAGE IDs
  // (stable identifiers). We build the bidirectional map once the
  // wiki document is available, then resolve the route slug to a
  // page id below.
  const { slugToId, idToSlug } = useMemo(() => {
    const slugToId = new Map<string, string>();
    const idToSlug = new Map<string, string>();
    const walk = (nodes: WikiPageNode[] | undefined): void => {
      for (const n of nodes ?? []) {
        if (n.slug) slugToId.set(n.slug, n.id);
        if (n.id) idToSlug.set(n.id, n.slug);
        if (n.children?.length) walk(n.children);
      }
    };
    walk(wiki?.structure?.pages);
    // The overview page lives on ``wiki.overview`` (not in the
    // structure tree) — register it explicitly so /wiki resolves
    // back to "overview" when the path is empty AND the explicit
    // /wiki/overview slug also resolves.
    if (wiki?.overview) {
      const ov = wiki.overview;
      if (ov.slug) slugToId.set(ov.slug, ov.id);
      if (ov.id) idToSlug.set(ov.id, ov.slug);
    }
    return { slugToId, idToSlug };
  }, [wiki?.structure?.pages, wiki?.overview]);

  // ── Active page id resolution ──────────────────────────────────────
  // No slug → overview. Slug present + resolvable → resolved id.
  // Slug present + unresolvable + wiki loaded → ``null`` (sentinel for
  // the "Page not found" placeholder rendered below). While the wiki
  // is still loading we hold the slug and resolve once it arrives.
  const activePageId = useMemo<string | null>(() => {
    if (!routeSlug) return "overview";
    if (!wiki) return null; // wait for load
    const resolved = slugToId.get(routeSlug);
    if (resolved) return resolved;
    // Tolerate the case where a caller passed a page-id where a slug
    // was expected (older deep-links emitted by the wiki graph
    // panel). Drop the prefix and try matching against id directly.
    if (idToSlug.has(routeSlug)) return routeSlug;
    return null;
  }, [routeSlug, wiki, slugToId, idToSlug]);

  const isPageNotFound = !!routeSlug && wiki !== null && activePageId === null;

  // ── Legacy ?page= scrub ────────────────────────────────────────────
  // Deep-links from older surfaces (the wiki graph preview panel,
  // bookmarks, marketing emails) still arrive as ``?page=<pageId>``.
  // Once the wiki document is available we map the legacy id → slug
  // and rewrite the URL to the new path-based shape, preserving every
  // OTHER query param (``view``, ``lang``, ``version``).
  useEffect(() => {
    if (!wiki || !channelId) return;
    const legacyPageParam = searchParams.get("page");
    if (!legacyPageParam) return;

    const slug = idToSlug.get(legacyPageParam);
    if (!slug) {
      // Unknown legacy id — strip the param but stay on overview to
      // avoid a 404 loop. The "Page not found" placeholder below is
      // for *path*-shaped slugs the user typed in directly, not
      // legacy query-string ids.
      const search = preserveQueryParams(searchParams, ["page"]);
      navigate(
        { pathname: location.pathname, search },
        { replace: true },
      );
      return;
    }
    const search = preserveQueryParams(searchParams, ["page"]);
    navigate(
      { pathname: buildWikiPath(channelId, slug), search },
      { replace: true },
    );
    // ``location.pathname`` and ``navigate`` are stable; we explicitly
    // re-fire when the wiki document or the page param changes so a
    // late-arriving wiki still triggers the scrub.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wiki, channelId, searchParams, idToSlug]);

  // Derive manual mode from the effective channel policy.
  // The Maintain Wiki button was removed in the action redesign — the
  // unified "Update wiki" primary action covers the same flow with
  // explicit user intent. The maintenance_mode policy is still read
  // elsewhere (auto-maintainer wiring); this component no longer needs
  // it directly.

  // Version history
  const { data: versions, isLoading: isVersionsLoading, refetch: refetchVersions } = useWikiVersions(channelId);
  const { data: versionData, isLoading: isVersionLoading } = useWikiVersion(
    channelId,
    viewingVersionNumber ?? undefined,
  );

  // Only fetch non-overview pages lazily (when not viewing a version).
  // ``activePageId`` is nullable when the route slug is unresolvable;
  // skip the fetch in that case so the "Page not found" placeholder
  // renders without an extra useless request.
  const lazyPageId =
    viewingVersionNumber === null &&
    activePageId !== null &&
    activePageId !== "overview"
      ? activePageId
      : undefined;
  const { data: pageData, isLoading: isPageLoading } = useWikiPage(channelId, lazyPageId, targetLang);

  const {
    mutate: triggerRefresh,
    isPending: isRefreshing,
    error: refreshError,
    generationStatus,
  } = useWikiRefresh(channelId, targetLang);

  const { status: extractionStatus } = useExtractionStatus(channelId, {
    isSyncing: isRefreshing,
  });
  const showEnrichmentRow =
    extractionStatus !== null &&
    ((extractionStatus.counts.pending ?? 0) > 0 ||
      (extractionStatus.counts.extracting ?? 0) > 0);

  // ── Three primary wiki actions ─────────────────────────────────────
  // Backend ``/wiki/refresh`` accepts ``mode={update|reorganize|rebuild}``.
  // Each handler calls the same pollable refresh flow so the
  // status/banner/version-history wiring stays uniform.
  //
  //   handleUpdate    → mode=update    — incremental refresh, keep folders
  //   handleReorganize→ mode=reorganize— refresh + re-plan folder boundaries
  //   handleRebuild   → mode=rebuild   — snapshot to history, wipe, regen
  const handleUpdate = useCallback(() => {
    triggerRefresh(
      () => {
        refetch();
        refetchVersions();
      },
      undefined,
      "update",
    );
  }, [triggerRefresh, refetch, refetchVersions]);

  const handleReorganize = useCallback(() => {
    triggerRefresh(
      () => {
        refetch();
        refetchVersions();
      },
      undefined,
      "reorganize",
    );
  }, [triggerRefresh, refetch, refetchVersions]);

  const handleRebuild = useCallback(() => {
    triggerRefresh(
      () => {
        refetch();
        refetchVersions();
      },
      undefined,
      "rebuild",
    );
  }, [triggerRefresh, refetch, refetchVersions]);

  // Backwards-compat alias — the WikiLayout still wires `onRefresh`
  // and the Sidebar header still calls handleRefresh as the primary
  // action. Update is the new primary, so route the legacy callsite
  // there.
  const handleRefresh = handleUpdate;

  // Sync completion should refresh wiki/memory presence in place so
  // empty-state CTAs transition to real content without navigation.
  useEffect(() => {
    if (!syncState?.job_id || syncState.state !== "idle") return;
    refetch();
    refetchVersions();
    refetchMemoryCount();
  }, [syncState?.job_id, syncState?.state, refetch, refetchVersions, refetchMemoryCount]);

  const handleRegenerateInLang = useCallback((lang: string) => {
    // Switch displayed language AND force a regeneration in that language.
    // Used by the empty-state "Generate" CTA — unambiguously means "make one".
    setTargetLang(lang);
    if (channelId) {
      try {
        localStorage.setItem(`wiki.targetLang.${channelId}`, lang);
      } catch {
        // Silently ignore — private-mode Safari throws on localStorage access
      }
    }
    triggerRefresh(() => {
      refetch();
      refetchVersions();
    }, lang);
  }, [channelId, triggerRefresh, refetch, refetchVersions]);

  // Track the last language the user asked to switch to. When the fetch for
  // that language resolves as 404 (wiki doesn't exist yet) we auto-regenerate;
  // when it resolves as 200 we just show the cached wiki. Cleared as soon as
  // the transition is handled so subsequent 404s (e.g. user returns to a page
  // they previously switched away from) don't silently re-trigger regens.
  const [pendingSwitchLang, setPendingSwitchLang] = useState<string | null>(null);

  const handleSwitchLang = useCallback((lang: string) => {
    // Dropdown picker path: switch displayed language. Only auto-regen if the
    // target language's wiki does not yet exist (404). Existing wikis in
    // another language are shown from cache without burning a generation.
    if (lang === targetLang) return;
    setTargetLang(lang);
    if (channelId) {
      try {
        localStorage.setItem(`wiki.targetLang.${channelId}`, lang);
      } catch {
        // Silently ignore — private-mode Safari throws on localStorage access
      }
    }
    setPendingSwitchLang(lang);
  }, [channelId, targetLang]);

  useEffect(() => {
    // Resolve a pending language switch once the new fetch has settled.
    if (!pendingSwitchLang || pendingSwitchLang !== targetLang) return;
    if (isLoading || isRefreshing) return;
    if (isNotFound) {
      // Wiki for this language doesn't exist — auto-regen.
      setPendingSwitchLang(null);
      triggerRefresh(() => {
        refetch();
        refetchVersions();
      }, targetLang);
    } else if (wiki) {
      // Wiki for this language exists — just show it.
      setPendingSwitchLang(null);
    }
  }, [
    pendingSwitchLang, targetLang, isLoading, isRefreshing, isNotFound, wiki,
    triggerRefresh, refetch, refetchVersions,
  ]);

  // Accepts either a page-id (e.g. "topic-auth", "folder-foo") OR a
  // raw slug (e.g. "topic-auth" without prefix, as the LLM emits in
  // See Also / Related / children TOC links). Resolves to a slug by
  // walking the structure tree, then navigates via path-based URL
  // ``/channels/:id/wiki/:slug`` so the address bar mirrors the
  // active page. Other query params (``view``, ``lang``, ``version``)
  // are preserved across the navigation.
  const handleNavigate = useCallback(
    (pageIdOrSlug: string) => {
      if (!pageIdOrSlug || !channelId) return;
      const pages = wiki?.structure?.pages ?? [];
      const walk = (nodes: WikiPageNode[]): WikiPageNode | null => {
        for (const n of nodes) {
          if (n.id === pageIdOrSlug || n.slug === pageIdOrSlug) return n;
          const inChild = walk(n.children ?? []);
          if (inChild) return inChild;
        }
        return null;
      };
      const node = walk(pages);
      // Navigating to the overview drops the slug segment entirely so
      // the URL reads ``/channels/{id}/wiki`` instead of
      // ``/channels/{id}/wiki/overview``.
      const targetSlug = node
        ? node.id === "overview"
          ? undefined
          : (node.slug ?? pageIdOrSlug)
        : pageIdOrSlug;
      const search = preserveQueryParams(searchParams);
      navigate(`${buildWikiPath(channelId, targetSlug)}${search}`);
    },
    [channelId, wiki?.structure?.pages, searchParams, navigate],
  );

  // Selecting a historical version writes ``?version=N`` to the URL
  // (via ``setViewingVersionNumber``). The user STAYS on whichever
  // page they were viewing — ``versionData.pages`` is a per-page
  // dict so the overlay works on any topic / folder, not just the
  // overview. ``preserveQueryParams`` carries ``?version`` across
  // subsequent slug navigations.
  const handleSelectVersion = useCallback(
    (versionNumber: number) => {
      setViewingVersionNumber(versionNumber);
    },
    [setViewingVersionNumber],
  );

  const handleBackToCurrent = useCallback(() => {
    setViewingVersionNumber(null);
  }, [setViewingVersionNumber]);

  const handleDownload = useCallback(async () => {
    try {
      const res = await authFetch(`${API_BASE}/api/channels/${channelId}/wiki/download`);
      if (!res.ok) {
        alert(`Download failed (${res.status})`);
        return;
      }
      const blob = await res.blob();
      const disposition = res.headers.get("Content-Disposition") || "";
      const nameMatch = disposition.match(/filename[^;=\n]*=["']?([^"';\n]+)/);
      const filename = nameMatch ? nameMatch[1].trim() : `${channelId}-wiki.md`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      alert("Download failed. Please try again.");
    }
  }, [channelId]);

  const handlePageDownload = useCallback(async (pageId: string, pageSlug?: string) => {
    try {
      const res = await authFetch(
        `${API_BASE}/api/channels/${channelId}/wiki/pages/${encodeURIComponent(pageId)}/download`,
      );
      if (!res.ok) {
        alert(`Page download failed (${res.status})`);
        return;
      }
      const blob = await res.blob();
      const disposition = res.headers.get("Content-Disposition") || "";
      const nameMatch = disposition.match(/filename[^;=\n]*=["']?([^"';\n]+)/);
      const filename = nameMatch
        ? nameMatch[1].trim()
        : `${pageSlug ?? pageId}-wiki.md`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("[WikiTab] Page download failed:", err);
      alert("Page download failed. Please try again.");
    }
  }, [channelId]);

  // Compute topicPages BEFORE the early returns below — React's rules of
  // hooks require ``useMemo`` to fire on every render, and the early
  // returns at the loading/empty/404 branches would otherwise skip it.
  // Use ``?.`` to tolerate the null wiki / null versionData states; the
  // empty-array fallback is what the early-return UIs would have used
  // anyway. Pairs with the ``useExtractionStatus`` last-key guard.
  const _structureForTopicPages =
    viewingVersionNumber !== null && versionData !== null
      ? versionData.structure
      : wiki?.structure;
  // Flatten the structure tree to collect EVERY topic page,
  // regardless of folder nesting. Before this, the Overview's topic
  // grid only showed root-level topics — when most topics live
  // inside folders (the planner's normal output), the grid showed
  // only the 2-3 loose orphans which felt broken to the user. Now
  // the grid is the canonical "all topics" view; folder cards still
  // give grouped navigation above.
  const topicPages = useMemo(() => {
    const collect = (
      nodes: WikiPageNode[] | undefined,
      out: WikiPageNode[],
    ): WikiPageNode[] => {
      for (const n of nodes ?? []) {
        if (n.page_type === "topic") out.push(n);
        if (n.children && n.children.length > 0) collect(n.children, out);
      }
      return out;
    };
    return collect(_structureForTopicPages?.pages, []);
  }, [_structureForTopicPages]);
  // Folder pages — surfaced on the Overview as a dedicated cards
  // section so the planner-produced grouping is visible at the entry
  // point, not just in the sidebar tree.
  const folderPages = useMemo(
    () =>
      _structureForTopicPages?.pages.filter((p) => p.page_type === "folder") ??
      [],
    [_structureForTopicPages],
  );
  const overviewGeneratedAt =
    viewingVersionNumber !== null && versionData !== null
      ? versionData.generated_at
      : wiki?.generated_at;

  // Graph view = full-width canvas, NO wiki sidebar. The pages-list
  // sidebar is irrelevant when the operator is in graph mode and only
  // steals horizontal real-estate. Early-return short-circuits the
  // whole WikiLayout chrome below. Channel hub + cytoscape don't need
  // wiki data, so this is safe even while ``wiki`` is still loading.
  if (view === "graph") {
    return (
      <div className="flex h-full flex-col min-h-0">
        <div className="flex items-center justify-between border-b border-border bg-card/60 px-5 py-2 shrink-0">
          <SegmentedToggle
            ariaLabel="Wiki view"
            value={view}
            options={WIKI_VIEW_OPTIONS}
            onChange={setView}
          />
        </div>
        <div className="flex-1 min-h-0">
          <FullscreenWrapper
            label="Enlarge graph"
            buttonPlacement="inline"
            buttonAlign="left"
            className="h-full"
          >
            <Suspense
              fallback={
                <div
                  className="flex h-full items-center justify-center text-sm text-muted-foreground"
                  data-testid="wiki-graph-suspense"
                >
                  Loading graph view…
                </div>
              }
            >
              <WikiGraph channelId={channelId} />
            </Suspense>
          </FullscreenWrapper>
        </div>
      </div>
    );
  }

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

  // 404: wiki has never been generated for this language — show a targeted empty state
  if (!wiki && isNotFound && !isRefreshing) {
    const supported = langConfig?.supported_languages ?? [targetLang];
    // ── PR-3 — phase-aware empty-state copy ─────────────────────────
    // The new ``/sync/status`` payload threads phases through
    // ``SyncState`` (see useSync.ts). When the auto-overview pipeline
    // is mid-flight we replace the static "No Wiki Yet" copy with a
    // dynamic message reflecting WHAT is currently happening, so the
    // user doesn't see a misleading "ready to generate" CTA while the
    // backend is already generating it.
    const phases = syncState?.phases ?? [];
    const overviewPhase = phases.find((p) => p.name === "overview_wiki");
    const wikiMaintPhase = phases.find((p) => p.name === "wiki_maintenance");
    const overviewState = overviewPhase?.state;
    const wikiMaintDone = wikiMaintPhase?.done ?? 0;

    // ``in_flight`` — auto-overview wiki is being generated right now.
    // Hide the manual Generate button (redundant) and the "Sync now"
    // CTA (sync already happened). The OverviewInFlightState renders a
    // live elapsed-time counter and a graduated Retry affordance so a
    // hung upstream call doesn't trap the user on this screen forever.
    //
    // Guard: only honour the in_flight state when this channel has
    // actually been synced (hasMemories === true). The backend's
    // ``_attempted`` set can carry stale ``in_flight`` reports across
    // channels in a process; rendering the spinner for never-synced
    // channels strands the user on a misleading screen. When the
    // channel has zero memories we drop through to the standard
    // "No Wiki Yet" empty state instead.
    if (overviewState === "in_flight" && hasMemories) {
      return (
        <OverviewInFlightState
          channelId={channelId}
          description={
            overviewPhase?.last_event_label ??
            "Beever Atlas is auto-generating the overview wiki for this channel."
          }
          startedAt={overviewPhase?.started_at}
        />
      );
    }

    // ``pending`` AND ``wiki_maintenance.done > 0`` — entity pages
    // exist, the overview is still queued. Surface the maintenance
    // progress AND keep the Generate button visible so the user can
    // publish before extraction completes if they want.
    if (overviewState === "pending" && wikiMaintDone > 0) {
      return (
        <PipelineEmptyState
          icon={BookOpen}
          title="Wiki being built"
          description={`${wikiMaintDone} entity page${
            wikiMaintDone === 1 ? "" : "s"
          } refreshed — overview wiki queued.`}
          steps={[
            { label: "Sync channel", icon: FolderSync, done: true, active: false },
            { label: "Build memories", icon: Sparkles, done: true, active: false },
            { label: "Generate wiki", icon: BookOpen, done: false, active: true },
          ]}
        >
          <WikiRegenerateButton
            currentLang={targetLang}
            supportedLanguages={supported}
            isRefreshing={isRefreshing}
            onRegenerate={() => handleRegenerateInLang(targetLang)}
            onRegenerateInLang={handleSwitchLang}
            label="Generate"
            size="lg"
          />
        </PipelineEmptyState>
      );
    }

    // RES-285 follow-up — "almost ready" state. Sync + extraction are
    // done (hasMemories=true) and overview_wiki is queued (pending) but
    // wiki_maintenance hasn't produced any entity pages yet
    // (wikiMaintDone=0). The auto-overview subscriber on the backend
    // will fire shortly; until it does, the previous behaviour showed
    // a misleading "No Wiki Yet" copy with a Generate CTA that didn't
    // tell the user the system was already going to build it for them.
    //
    // We narrow on `overviewState === "pending"` specifically (NOT
    // `undefined`) — undefined means the backend has no phase report,
    // which is the legacy / feature-flag-off path where auto-overview
    // is NOT coming, so the default "click Generate" CTA is correct.
    if (overviewState === "pending" && wikiMaintDone === 0 && hasMemories) {
      return (
        <PipelineEmptyState
          icon={Sparkles}
          title="Wiki will start shortly"
          description="Sync and extraction are complete — the wiki will start building automatically. You can also click Generate to start it now."
          steps={[
            { label: "Sync channel", icon: FolderSync, done: true, active: false },
            { label: "Build memories", icon: Sparkles, done: true, active: false },
            { label: "Generate wiki", icon: BookOpen, done: false, active: true },
          ]}
        >
          <WikiRegenerateButton
            currentLang={targetLang}
            supportedLanguages={supported}
            isRefreshing={isRefreshing}
            onRegenerate={() => handleRegenerateInLang(targetLang)}
            onRegenerateInLang={handleSwitchLang}
            label="Generate"
            size="lg"
          />
        </PipelineEmptyState>
      );
    }

    // Default path — `undefined` overviewState (legacy backend, feature
    // flag off, or stale data), `skipped`, or any case we didn't catch
    // above. Falls back to the original "Sync / Generate CTA depending
    // on whether the channel has any memories" UX.
    //
    // sync-monitor-redesign — hide the Generate CTA while any pipeline
    // phase is ``in_flight``. The SyncProgressV2 card above already
    // tells the user the wiki is being built; showing a "Generate"
    // button at the same time teases an action they can't usefully take.
    //
    // Guard: on never-synced channels (isNoMemory === true), ignore any
    // ``in_flight`` phase reports. The backend's per-process subscriber
    // state (AutoOverviewSubscriber._attempted, ExtractionWorker queue
    // residue) can bleed stale ``in_flight`` signals onto channels that
    // never started a pipeline. Without this guard, never-synced
    // channels render the "Pipeline in progress" message AND lose
    // the "Sync Channel Now" CTA, stranding the user on a screen with
    // no actionable next step.
    const isPipelineInFlight =
      !isNoMemory && phases.some((p) => p.state === "in_flight");
    // Previously we ``return null`` here on the rationale that the
    // monitor card at the top of the page already shows progress. But
    // when the monitor is COLLAPSED, the user sees only a compact
    // stepper strip and a blank canvas below it — caught by UI testing
    // as "blank page when sync and monitoring is running". Fall through
    // to the phase-aware PipelineEmptyState below so the wiki tab body
    // always shows SOMETHING informative.
    const steps = [
      { label: "Sync channel", icon: FolderSync, done: !isNoMemory, active: isNoMemory },
      { label: "Build memories", icon: Sparkles, done: !isNoMemory, active: false },
      { label: "Generate wiki", icon: BookOpen, done: false, active: !isNoMemory },
    ];
    return (
      <PipelineEmptyState
        icon={BookOpen}
        title={
          isPipelineInFlight
            ? "Pipeline in progress"
            : isNoMemory
              ? "Build your channel wiki"
              : wikiT(targetLang, "noWikiYet")
        }
        description={
          isPipelineInFlight
            ? "The activity stream above shows what's happening live."
            : isNoMemory
              ? "Turn conversations into a structured wiki with topics, decisions, and references."
              : wikiT(targetLang, "noWikiEmptySubtitle")
        }
        steps={steps}
        primaryActionLabel={
          !isPipelineInFlight && isNoMemory ? "Sync Channel Now" : undefined
        }
        onPrimaryAction={
          !isPipelineInFlight && isNoMemory && triggerSync
            ? () => void triggerSync()
            : undefined
        }
        primaryActionDisabled={!triggerSync || !!isSyncing}
        primaryActionLoading={!!isSyncing}
        secondaryActionLabel={
          !isPipelineInFlight && isNoMemory ? "View sync history" : undefined
        }
        onSecondaryAction={
          !isPipelineInFlight && isNoMemory
            ? () => navigate(`/channels/${channelId}/sync-history`)
            : undefined
        }
        secondaryActionVariant="link"
      >
        {isPipelineInFlight || isNoMemory ? (
          <></>
        ) : (
          <WikiRegenerateButton
            currentLang={targetLang}
            supportedLanguages={supported}
            isRefreshing={isRefreshing}
            onRegenerate={() => handleRegenerateInLang(targetLang)}
            onRegenerateInLang={handleSwitchLang}
            label="Generate"
            size="lg"
          />
        )}
      </PipelineEmptyState>
    );
  }

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

  // When viewing a version, use that version's data
  const isViewingVersion = viewingVersionNumber !== null && versionData !== null;

  const activeStructure = isViewingVersion ? versionData.structure : wiki.structure;
  const activeOverview = isViewingVersion ? versionData.overview : wiki.overview;

  // Resolve the active page. ``activePageId`` is null when the route
  // slug doesn't match any known page (the "Page not found" path);
  // we fall back to ``null`` so the placeholder renders below.
  let activePage: WikiPage | null;
  if (activePageId === null) {
    activePage = null;
  } else if (isViewingVersion) {
    activePage = activePageId === "overview"
      ? activeOverview
      : (versionData.pages[activePageId] ?? null);
  } else {
    activePage = activePageId === "overview" ? wiki.overview : (pageData ?? null);
  }

  // ``topicPages`` is hoisted above the early returns above (rules of
  // hooks). It uses a safe optional-chain on the structure, so the
  // computation is identical here once we reach this point.

  const showPageLoading = isViewingVersion ? isVersionLoading : isPageLoading;

  // The language currently on screen — matches what the top bar chip shows.
  // Historical versions must never borrow the current session's targetLang;
  // fall back to "en" when a legacy version record lacks target_lang.
  const displayedLang = isViewingVersion
    ? (versionData?.target_lang ?? "en")
    : targetLang;

  // Show a loading indicator while fetching; an empty-state when the backend
  // returns 404 (page exists in sidebar structure but has no cached content)
  // so the pane does not spin forever.
  let pageContent: ReactNode;
  if (showPageLoading) {
    pageContent = (
      <div className="flex items-center justify-center py-16">
        <RefreshCw className="w-5 h-5 animate-spin text-muted-foreground" />
      </div>
    );
  } else if (isPageNotFound) {
    // Path-based slug didn't resolve to any page in the loaded wiki —
    // typically a stale bookmark or a typo'd URL. Lightweight
    // placeholder rather than a redirect so the user can see WHICH
    // slug failed and act on it.
    pageContent = (
      <div
        className="flex flex-col items-center justify-center py-16 text-center"
        data-testid="wiki-page-not-found"
      >
        <FileText className="w-8 h-8 text-muted-foreground/40 mb-3" />
        <p className="text-sm text-muted-foreground">
          Page not found
        </p>
        <p className="mt-1 text-xs text-muted-foreground/70">
          No wiki page matches “{routeSlug}”.
        </p>
      </div>
    );
  } else if (!activePage) {
    pageContent = (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <FileText className="w-8 h-8 text-muted-foreground/40 mb-3" />
        <p className="text-sm text-muted-foreground">
          This page isn't available in the current wiki.
        </p>
        <p className="mt-1 text-xs text-muted-foreground/70">
          Try regenerating the wiki to populate it.
        </p>
      </div>
    );
  } else {
    // The opacity-fade-during-revalidation pattern was the actual cause
    // of the long-running "wiki page flashes every few minutes" bug.
    // ``isPageRevalidating`` flips true on EVERY poll cycle (independent
    // of whether content actually changed), so the wrapper produced a
    // 100→60→100 flash even when the lastKeyRef guard correctly skipped
    // the data swap. The guard already prevents content tearing, so the
    // wrapper is pure noise. Render the page directly.
    const renderedPage = renderPage(
      activePage,
      topicPages,
      handleNavigate,
      displayedLang,
      folderPages,
      overviewGeneratedAt,
    );
    // Per-page download button — shown for every page except the overview
    // (the top-bar "Download wiki" button covers the overview export).
    // Icon-only, small, positioned at the top-right of the content column
    // so it stays out of the way of the page heading.
    const isOverviewPage =
      activePage.id === "overview" ||
      (activePage.page_type === "fixed" && activePage.slug === "overview");
    pageContent = isOverviewPage ? renderedPage : (
      <div className="relative">
        <div className="absolute right-0 top-0 z-10 flex items-center gap-2">
          {channelId && (
            <CurationDropdown
              channelId={channelId}
              slug={activePage.slug}
              curationMode={
                (activePage.curation_mode as "auto" | "manual" | "frozen") ?? "auto"
              }
              targetLang={displayedLang}
            />
          )}
          <button
            type="button"
            title="Download this page as Markdown"
            aria-label="Download page"
            onClick={() => void handlePageDownload(activePage.id, activePage.slug)}
            className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-muted/40 px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            <Download size={12} />
            <span className="hidden sm:inline">Export page</span>
          </button>
        </div>
        {renderedPage}
      </div>
    );
  }

  return (
    <WikiLayout
      channelId={channelId!}
      structure={activeStructure}
      activePage={activePage ?? activeOverview}
      onNavigate={handleNavigate}
      onRefresh={handleRefresh}
      isRefreshing={isRefreshing}
      versionCount={wiki.version_count ?? 0}
      versions={versions}
      isVersionsLoading={isVersionsLoading}
      viewingVersionNumber={viewingVersionNumber}
      onSelectVersion={handleSelectVersion}
      onBackToCurrent={handleBackToCurrent}
      currentLang={displayedLang}
      supportedLanguages={langConfig?.supported_languages ?? [targetLang]}
      onRegenerateInLang={handleSwitchLang}
      versionHistoryOpen={versionHistoryOpen}
      onVersionHistoryToggle={() => setVersionHistoryOpen((v) => !v)}
      viewToggle={
        <SegmentedToggle
          ariaLabel="Wiki view"
          value={view}
          options={WIKI_VIEW_OPTIONS}
          onChange={setView}
        />
      }
      headerExplainer={
        <ViewExplainerButton
          heading="How the wiki works"
          sections={WIKI_EXPLAINER_SECTIONS}
          triggerLabel="Learn what Wiki and WikiGraph mean"
        />
      }
      headerExtra={
        <WikiHealthToolbar
          channelId={channelId!}
          onDownload={handleDownload}
          onHistoryToggle={() => setVersionHistoryOpen((v) => !v)}
          historyOpen={versionHistoryOpen}
          versionCount={wiki?.version_count ?? 0}
          onReorganize={handleReorganize}
          onRebuild={handleRebuild}
          isRegenerating={isRefreshing}
        />
      }
    >
      <>
        {viewingVersionNumber !== null && versionData && (
          <div className="mb-5 flex items-center justify-between rounded-xl border border-blue-500/30 bg-blue-500/10 px-4 py-3">
            <div className="flex items-center gap-2 text-sm text-blue-600 dark:text-blue-400">
              <HistoryIcon className="h-4 w-4" />
              <span>
                Viewing <span className="font-semibold">Version {versionData.version_number}</span>
                {" — "}
                generated {new Date(versionData.generated_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}
              </span>
            </div>
            <button
              onClick={handleBackToCurrent}
              className="rounded-md bg-blue-500/20 px-3 py-1 text-xs font-medium text-blue-600 hover:bg-blue-500/30 dark:text-blue-400 transition-colors"
            >
              Back to current
            </button>
          </div>
        )}
        {viewingVersionNumber === null && isRefreshing && generationStatus && generationStatus.status === "running" && (
          <WikiRegeneratingBanner status={generationStatus} />
        )}
        {showEnrichmentRow && extractionStatus && (
          <div
            data-testid="enrichment-status-row"
            role="status"
            aria-live="polite"
            className="mb-4 flex items-center gap-2 rounded-lg border border-primary/20 bg-primary/5 px-3 py-2 text-sm text-foreground"
          >
            <Loader2 className="h-4 w-4 animate-spin text-primary shrink-0" />
            <span>
              Enriching{" "}
              <strong>
                {(extractionStatus.counts.pending ?? 0) + (extractionStatus.counts.extracting ?? 0)}
              </strong>{" "}
              of <strong>{extractionStatus.total}</strong> messages &mdash; wiki refresh queued
            </span>
          </div>
        )}
        {pageContent}
      </>
    </WikiLayout>
  );
}
