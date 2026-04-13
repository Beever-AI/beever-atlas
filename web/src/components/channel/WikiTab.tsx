import { useState, useCallback, useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { wikiT } from "@/lib/wikiI18n";
import { RefreshCw, BookOpen, AlertTriangle, Sparkles, Network, FileText, Loader2, CheckCircle2, Circle, ArrowRight, FolderSync, History as HistoryIcon } from "lucide-react";
import { useWiki } from "@/hooks/useWiki";
import { useWikiPage } from "@/hooks/useWikiPage";
import { useWikiRefresh, type WikiGenerationStatus } from "@/hooks/useWikiRefresh";
import { useWikiVersions } from "@/hooks/useWikiVersions";
import { useWikiVersion } from "@/hooks/useWikiVersion";
import { useChannelMemoryCount } from "@/hooks/useChannelMemoryCount";
import { WikiLayout } from "@/components/wiki/WikiLayout";
import { OverviewPage } from "@/components/wiki/OverviewPage";
import { TopicPage } from "@/components/wiki/TopicPage";
import { GenericPage } from "@/components/wiki/GenericPage";
import { FaqPage } from "@/components/wiki/FaqPage";
import { WikiRegenerateButton } from "@/components/channel/WikiRegenerateButton";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { WikiPage, WikiPageNode } from "@/lib/types";

interface LanguageConfig {
  supported_languages: string[];
  default_target_language: string;
}

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

function renderPage(
  page: WikiPage,
  topicPages: WikiPageNode[],
  onNavigate: (pageId: string) => void,
  lang?: string,
) {
  if (page.id === "overview" || (page.page_type === "fixed" && page.slug === "overview")) {
    return <OverviewPage page={page} topicPages={topicPages} onNavigate={onNavigate} lang={lang} />;
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
  const { id: channelId } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [activePageId, setActivePageId] = useState<string>("overview");
  const [viewingVersionNumber, setViewingVersionNumber] = useState<number | null>(null);

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
  const { hasMemories, isLoading: isMemoryCountLoading } = useChannelMemoryCount(channelId);

  // Version history
  const { data: versions, isLoading: isVersionsLoading, refetch: refetchVersions } = useWikiVersions(channelId);
  const { data: versionData, isLoading: isVersionLoading } = useWikiVersion(
    channelId,
    viewingVersionNumber ?? undefined,
  );

  // Only fetch non-overview pages lazily (when not viewing a version)
  const lazyPageId = viewingVersionNumber === null && activePageId !== "overview" ? activePageId : undefined;
  const { data: pageData, isLoading: isPageLoading } = useWikiPage(channelId, lazyPageId, targetLang);

  const {
    mutate: triggerRefresh,
    isPending: isRefreshing,
    error: refreshError,
    generationStatus,
  } = useWikiRefresh(channelId, targetLang);

  const handleRefresh = useCallback(() => {
    triggerRefresh(() => {
      refetch();
      refetchVersions();
    });
  }, [triggerRefresh, refetch, refetchVersions]);

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

  const handleNavigate = useCallback((pageId: string) => {
    setActivePageId(pageId);
  }, []);

  const handleSelectVersion = useCallback((versionNumber: number) => {
    setViewingVersionNumber(versionNumber);
    setActivePageId("overview");
  }, []);

  const handleBackToCurrent = useCallback(() => {
    setViewingVersionNumber(null);
    setActivePageId("overview");
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

  // 404: wiki has never been generated for this language — show a targeted empty state
  if (!wiki && isNotFound && !isRefreshing) {
    const supported = langConfig?.supported_languages ?? [targetLang];
    return (
      <div className="flex h-full min-h-0 items-center justify-center px-6 py-12">
        <div className="mx-auto w-full max-w-md text-center">
          <div className="mx-auto mb-5 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
            <BookOpen className="h-5 w-5 text-muted-foreground" />
          </div>
          <h3 className="text-base font-semibold text-foreground">
            {wikiT(targetLang, "noWikiYet")}
          </h3>
          <p className="mx-auto mt-1.5 max-w-xs text-sm text-muted-foreground">
            {wikiT(targetLang, "noWikiEmptySubtitle")}
          </p>
          <div className="mt-6 flex justify-center">
            <WikiRegenerateButton
              currentLang={targetLang}
              supportedLanguages={supported}
              isRefreshing={isRefreshing}
              onRegenerate={() => handleRegenerateInLang(targetLang)}
              onRegenerateInLang={handleSwitchLang}
              label="Generate"
              size="lg"
            />
          </div>
        </div>
      </div>
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

  // Resolve the active page
  let activePage: WikiPage | null;
  if (isViewingVersion) {
    activePage = activePageId === "overview"
      ? activeOverview
      : (versionData.pages[activePageId] ?? null);
  } else {
    activePage = activePageId === "overview" ? wiki.overview : (pageData ?? null);
  }

  const topicPages = activeStructure.pages.filter((p) => p.page_type === "topic");

  const showPageLoading = isViewingVersion ? isVersionLoading : isPageLoading;

  // The language currently on screen — matches what the top bar chip shows.
  // Historical versions must never borrow the current session's targetLang;
  // fall back to "en" when a legacy version record lacks target_lang.
  const displayedLang = isViewingVersion
    ? (versionData?.target_lang ?? "en")
    : targetLang;

  // Show a loading indicator inside the layout when fetching a non-overview page
  const pageContent =
    showPageLoading || !activePage ? (
      <div className="flex items-center justify-center py-16">
        <RefreshCw className="w-5 h-5 animate-spin text-muted-foreground" />
      </div>
    ) : (
      renderPage(activePage, topicPages, handleNavigate, displayedLang)
    );

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
        {pageContent}
      </>
    </WikiLayout>
  );
}
