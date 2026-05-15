import { useEffect, useState } from "react";
import { useNavigate, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { RefreshCw, Sparkles, Brain, Network, Layers, FileText, Zap, FolderSync } from "lucide-react";
import { useMemories } from "@/hooks/useMemories";
import { useTopics } from "@/hooks/useTopics";
import { useChannelSummary } from "@/hooks/useChannelSummary";
import { useChannelMemoryCount } from "@/hooks/useChannelMemoryCount";
import { api, ApiError } from "@/lib/api";
import { MemoryFilters } from "./MemoryFilters";
import { FactCard } from "./FactCard";
import { Pagination } from "./Pagination";
import { SummaryCard } from "./SummaryCard";
import { ClusterCard } from "./ClusterCard";
import { MemoryGraphView } from "./MemoryGraphView";
import { SegmentedToggle } from "@/components/shared/SegmentedToggle";
import { ViewExplainerButton, type ExplainerSection } from "@/components/shared/ViewExplainerButton";
import { PipelineEmptyState } from "@/components/shared/PipelineEmptyState";
import type { SyncState } from "@/hooks/useSync";

type View = "memory" | "graph";

const VIEW_OPTIONS = [
  { value: "memory" as const, label: "3-Tier Memory", icon: Brain, testId: "memory-view-toggle-memory" },
  { value: "graph" as const, label: "Memory Graph", icon: Network, testId: "memory-view-toggle-graph" },
];

// Plain-English explanations for the toggle, surfaced via the
// ViewExplainerButton next to it. Operators new to the surface get the
// full mental model in one click without having to read code or docs.
const VIEW_EXPLAINER_SECTIONS: ExplainerSection[] = [
  {
    title: "3-Tier Memory",
    icon: Brain,
    accent: "bg-primary/15 text-primary",
    tagline: "Knowledge organized from the bird's-eye view down to the raw fact.",
    body: (
      <>
        <p>
          Each tier zooms in: the top is the channel-wide story, the
          middle clusters knowledge by topic, and the bottom is every
          individual statement we extracted.
        </p>
        <ul className="space-y-2.5 pt-1">
          <li className="flex items-start gap-2.5">
            <span className="shrink-0 mt-0.5 flex h-5 w-5 items-center justify-center rounded bg-sky-500/15 text-sky-500">
              <Layers size={11} />
            </span>
            <span className="flex-1">
              <strong className="text-foreground">Tier 0 — Channel Summary.</strong>{" "}
              One-page narrative of the whole channel: themes, momentum,
              top people, and recent activity.
            </span>
          </li>
          <li className="flex items-start gap-2.5">
            <span className="shrink-0 mt-0.5 flex h-5 w-5 items-center justify-center rounded bg-emerald-500/15 text-emerald-500">
              <FileText size={11} />
            </span>
            <span className="flex-1">
              <strong className="text-foreground">Tier 1 — Topic Clusters.</strong>{" "}
              Knowledge grouped by topic — key facts, decisions, people,
              technologies, and FAQs in one place.
            </span>
          </li>
          <li className="flex items-start gap-2.5">
            <span className="shrink-0 mt-0.5 flex h-5 w-5 items-center justify-center rounded bg-amber-500/15 text-amber-500">
              <Zap size={11} />
            </span>
            <span className="flex-1">
              <strong className="text-foreground">Tier 2 — Atomic Facts.</strong>{" "}
              Individual statements with author, timestamp, quality
              score, and a permalink back to the source message.
            </span>
          </li>
        </ul>
      </>
    ),
  },
  {
    title: "Memory Graph",
    icon: Network,
    accent: "bg-emerald-500/15 text-emerald-500",
    tagline: "Visual map of how entities relate to each other.",
    body: (
      <>
        <p>
          Every person, project, and technology mentioned in the channel
          becomes a node. Edges connect them based on co-occurrence and
          extracted relationships.
        </p>
        <p>
          Click a node to see its neighborhood — who knows what, who
          works with whom, what depends on what. Use the floating filter
          panel to narrow the view by entity type or relationship.
        </p>
      </>
    ),
  },
];

export function TierBrowser() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { triggerSync, isSyncing, syncState } = useOutletContext<{
    triggerSync?: () => Promise<void>;
    isSyncing?: boolean;
    syncState?: SyncState;
  }>();
  const channelId = id ?? "";
  const [searchParams, setSearchParams] = useSearchParams();
  // URL-stateful view toggle so refreshing the page or sharing a link
  // preserves the operator's chosen surface (memory tiers vs entity
  // graph). The path stays ``/channels/:id/memories``; only ``?view``
  // flips. Replace history so back-button doesn't accumulate noise.
  const view: View = searchParams.get("view") === "graph" ? "graph" : "memory";
  const setView = (next: View) => {
    const updated = new URLSearchParams(searchParams);
    if (next === "memory") {
      updated.delete("view");
    } else {
      updated.set("view", next);
    }
    setSearchParams(updated, { replace: true });
  };

  const {
    facts,
    total: factsTotal,
    page: factsPage,
    pages: factsPages,
    pageSize: factsPageSize,
    goToPage: goToFactsPage,
    filters,
    setFilters,
    isLoading,
    refetch: refetchFacts,
  } = useMemories(channelId);
  const { clusters, isLoading: clustersLoading, error: clustersError, refetch: refetchTopics } = useTopics(channelId);
  const { summary, isLoading: summaryLoading, error: summaryError, refetch: refetchSummary } = useChannelSummary(channelId);
  const { hasMemories, isLoading: isMemoryCountLoading, refetch: refetchMemoryCount } = useChannelMemoryCount(channelId);

  const [consolidating, setConsolidating] = useState(false);
  const [showRefresh, setShowRefresh] = useState(false);
  const [consolidateMsg, setConsolidateMsg] = useState("");
  const [graphRefreshNonce, setGraphRefreshNonce] = useState(0);

  const handleConsolidate = async () => {
    setConsolidating(true);
    setConsolidateMsg("");
    try {
      await api.post(`/api/channels/${channelId}/consolidate`);
      setConsolidateMsg("Consolidation started. Refresh in a few minutes to see updated results.");
      setShowRefresh(true);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Failed to start consolidation";
      setConsolidateMsg(msg);
    } finally {
      setConsolidating(false);
    }
  };

  const handleRefresh = () => {
    refetchFacts();
    refetchTopics();
    refetchSummary();
    refetchMemoryCount();
    setShowRefresh(false);
    setConsolidateMsg("");
  };

  useEffect(() => {
    if (!syncState?.job_id || syncState.state !== "idle") return;
    refetchFacts();
    refetchTopics();
    refetchSummary();
    refetchMemoryCount();
    setGraphRefreshNonce((n) => n + 1);
  }, [syncState?.job_id, syncState?.state, refetchFacts, refetchTopics, refetchSummary, refetchMemoryCount]);

  // Graph view is delegated entirely to MemoryGraphView — it owns its
  // own loading / error / empty states. The toggle floats in a slim
  // header so the operator can flip back to the memory tiers without
  // a full tab change.
  if (view === "graph") {
    return (
      <div className="flex flex-col h-full min-h-0">
        <div className="flex items-center justify-between border-b border-border bg-card/60 px-5 py-2 shrink-0">
          <SegmentedToggle
            ariaLabel="Agent Memory view"
            value={view}
            options={VIEW_OPTIONS}
            onChange={setView}
          />
          <ViewExplainerButton
            heading="How agent memory works"
            sections={VIEW_EXPLAINER_SECTIONS}
            triggerLabel="Learn what 3-Tier Memory and Memory Graph mean"
          />
        </div>
        <div className="flex-1 min-h-0">
          <MemoryGraphView
            channelId={channelId}
            refreshNonce={graphRefreshNonce}
            onSyncNow={triggerSync}
            isSyncing={!!isSyncing}
            onViewSyncHistory={() => navigate(`/channels/${channelId}/sync-history`)}
          />
        </div>
      </div>
    );
  }

  if (isLoading && summaryLoading && clustersLoading) {
    return (
      <div className="flex flex-col h-full min-h-0">
        <div className="flex items-center justify-between border-b border-border bg-card/60 px-5 py-2 shrink-0">
          <SegmentedToggle
            ariaLabel="Agent Memory view"
            value={view}
            options={VIEW_OPTIONS}
            onChange={setView}
          />
          <ViewExplainerButton
            heading="How agent memory works"
            sections={VIEW_EXPLAINER_SECTIONS}
            triggerLabel="Learn what 3-Tier Memory and Memory Graph mean"
          />
        </div>
        <div className="p-6 text-center text-base text-muted-foreground">
          Loading memories...
        </div>
      </div>
    );
  }

  if (!isMemoryCountLoading && !hasMemories) {
    const steps = [
      { label: "Sync channel", icon: FolderSync, done: false, active: true },
      { label: "Extract memory", icon: Sparkles, done: false, active: false },
      { label: "Organize topics & facts", icon: Brain, done: false, active: false },
    ];
    return (
      <div className="flex flex-col h-full min-h-0">
        <div className="flex items-center justify-between border-b border-border bg-card/60 px-5 py-2 shrink-0">
          <SegmentedToggle
            ariaLabel="Agent Memory view"
            value={view}
            options={VIEW_OPTIONS}
            onChange={setView}
          />
          <ViewExplainerButton
            heading="How agent memory works"
            sections={VIEW_EXPLAINER_SECTIONS}
            triggerLabel="Learn what 3-Tier Memory and Memory Graph mean"
          />
        </div>
        <div className="flex-1 min-h-0">
          <PipelineEmptyState
            icon={Brain}
            title="Build team memory"
            description="Capture what matters from chat: topics, facts, and who knows what."
            steps={steps}
            primaryActionLabel="Sync Channel Now"
            onPrimaryAction={triggerSync ? () => void triggerSync() : undefined}
            primaryActionDisabled={!triggerSync || !!isSyncing}
            primaryActionLoading={!!isSyncing}
            secondaryActionLabel="View sync history"
            onSecondaryAction={() => navigate(`/channels/${channelId}/sync-history`)}
            secondaryActionVariant="link"
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-center justify-between border-b border-border bg-card/60 px-5 py-2 shrink-0 gap-4">
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <SegmentedToggle
            ariaLabel="Agent Memory view"
            value={view}
            options={VIEW_OPTIONS}
            onChange={setView}
          />
          {/* Quick-jump pills sit on the same row as the view toggle.
              Only meaningful on the 3-Tier surface (the Memory Graph
              view is one canvas, no internal anchors). */}
          <span aria-hidden className="h-5 w-px bg-border/60 shrink-0" />
          <MemoryQuickJump
            sections={[
              { id: "tier0-summary", label: "Channel", icon: Layers },
              { id: "tier1-topics", label: "Topics", icon: FileText, count: clusters.length },
              { id: "tier2-facts", label: "Atomic facts", icon: Zap, count: factsTotal },
            ]}
          />
        </div>
        <ViewExplainerButton
          heading="How agent memory works"
          sections={VIEW_EXPLAINER_SECTIONS}
          triggerLabel="Learn what 3-Tier Memory and Memory Graph mean"
        />
      </div>
      <div className="flex-1 min-h-0 overflow-auto p-4 sm:p-6 space-y-5 animate-fade-in">
        <div className="max-w-6xl mx-auto space-y-5">
        {/* Actions bar */}
        <div className="flex items-center justify-end gap-2">
        {showRefresh && (
          <button
            onClick={handleRefresh}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-primary/30 text-primary hover:bg-primary/10 transition-colors"
          >
            <RefreshCw size={14} />
            Refresh results
          </button>
        )}
        <button
          onClick={handleConsolidate}
          disabled={consolidating || !channelId}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-border text-muted-foreground hover:bg-muted/60 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <Sparkles size={14} />
          {consolidating ? "Starting..." : "Reconsolidate"}
        </button>
      </div>

      {consolidateMsg && (
        <div className="rounded-lg border border-border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">
          {consolidateMsg}
        </div>
      )}

      {/* Tier 0 — Channel Summary */}
      <div id="tier0-summary" className="scroll-mt-20">
      {summaryLoading ? (
        <div className="rounded-xl border border-border bg-card px-5 py-4 text-sm text-muted-foreground animate-pulse">
          Loading channel summary...
        </div>
      ) : summaryError ? (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-5 py-4 text-sm text-destructive">
          Failed to load channel summary.
        </div>
      ) : summary ? (
        <SummaryCard summary={summary} />
      ) : (
        <div className="rounded-xl border border-dashed border-border px-5 py-4 text-sm text-muted-foreground">
          No channel summary yet. Run consolidation to generate one.
        </div>
      )}
      </div>

      {/* Tier 1 — Topic Clusters */}
      <div id="tier1-topics" className="space-y-3 scroll-mt-20">
        <div className="flex items-end justify-between">
          <div>
            <h3 className="font-heading text-[28px] leading-tight text-foreground">
              Topics
            </h3>
            <p className="text-sm text-muted-foreground mt-1">
              Knowledge organized by topic.
            </p>
          </div>
          {clusters.length > 0 && (
            <span className="text-sm text-muted-foreground">
              {clusters.length} topics
            </span>
          )}
        </div>

        {clustersLoading ? (
          <div className="rounded-xl border border-border bg-card px-5 py-4 text-sm text-muted-foreground animate-pulse">
            Loading topic clusters...
          </div>
        ) : clustersError ? (
          <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-5 py-4 text-sm text-destructive">
            Failed to load topic clusters.
          </div>
        ) : clusters.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border px-5 py-4 text-sm text-muted-foreground">
            No topic clusters yet. Sync and consolidate to organize knowledge.
          </div>
        ) : (
          <div className="space-y-3">
            {clusters.map((c, idx) => (
              <div
                key={c.id}
                className="motion-safe:animate-rise-in"
                style={{ animationDelay: `${Math.min(idx, 10) * 35}ms` }}
              >
                <ClusterCard cluster={c} facts={facts} />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Tier 2 — Atomic facts */}
      <div id="tier2-facts" className="space-y-4 scroll-mt-20">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h3 className="font-heading text-[28px] leading-tight text-foreground">
              Atomic Facts
            </h3>
            <p className="text-sm text-muted-foreground mt-1">
              Individual knowledge extracted from this channel.
            </p>
          </div>
          <span className="text-sm text-muted-foreground">
            {factsTotal.toLocaleString()} matching facts
          </span>
        </div>

        <MemoryFilters filters={filters} setFilters={setFilters} />

        {factsTotal === 0 ? (
          <div className="rounded-xl border border-dashed border-border px-5 py-10 text-center text-sm text-muted-foreground">
            No memories yet. Sync this channel to start extracting knowledge.
          </div>
        ) : (
          <>
            <Pagination
              page={factsPage}
              pages={factsPages}
              total={factsTotal}
              pageSize={factsPageSize}
              onPage={goToFactsPage}
              disabled={isLoading}
            />
            <div className="space-y-3">
              {facts.map((fact, idx) => (
                <div
                  key={fact.id}
                  className="motion-safe:animate-rise-in"
                  style={{ animationDelay: `${Math.min(idx, 10) * 35}ms` }}
                >
                  <FactCard fact={fact} />
                </div>
              ))}
            </div>
            <Pagination
              page={factsPage}
              pages={factsPages}
              total={factsTotal}
              pageSize={factsPageSize}
              onPage={(p: number) => {
                goToFactsPage(p);
                document
                  .getElementById("tier2-facts")
                  ?.scrollIntoView({ behavior: "smooth", block: "start" });
              }}
              disabled={isLoading}
            />
          </>
        )}
      </div>
        </div>
      </div>
    </div>
  );
}

// ─── MemoryQuickJump ──────────────────────────────────────────────────────────
// Sticky pill row that lets the operator jump straight to Channel
// Summary / Topics / Atomic facts without scrolling. The active pill
// highlights based on which section is currently in view (via
// IntersectionObserver). Click → smooth-scroll to that section.

interface QuickJumpSection {
  id: string;
  label: string;
  icon: React.ComponentType<{ className?: string; size?: number }>;
  count?: number;
}

function MemoryQuickJump({ sections }: { sections: QuickJumpSection[] }) {
  const [activeId, setActiveId] = useState(sections[0]?.id ?? "");

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        // Pick the topmost section that's at least partially visible.
        // rootMargin biases toward "the section the user just scrolled
        // INTO" rather than the one they're scrolling out of.
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible.length > 0) {
          setActiveId(visible[0].target.id);
        }
      },
      { rootMargin: "-15% 0px -70% 0px", threshold: 0 },
    );
    sections.forEach((s) => {
      const el = document.getElementById(s.id);
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
  }, [sections]);

  const handleJump = (id: string) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    setActiveId(id);
  };

  return (
    <div
      className="flex items-center gap-1 overflow-x-auto min-w-0"
      role="navigation"
      aria-label="Memory page sections"
    >
      {sections.map((s) => {
        const isActive = activeId === s.id;
        return (
          <button
            key={s.id}
            type="button"
            onClick={() => handleJump(s.id)}
            className={`group inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[12px] font-medium transition-colors shrink-0 ${
              isActive
                ? "bg-primary/15 text-primary"
                : "text-muted-foreground hover:bg-muted hover:text-foreground"
            }`}
          >
            <s.icon
              size={13}
              className={`shrink-0 transition-colors ${
                isActive ? "text-primary" : "text-muted-foreground/70 group-hover:text-foreground"
              }`}
            />
            <span>{s.label}</span>
            {typeof s.count === "number" && (
              <span
                className={`ml-0.5 rounded-full px-1.5 py-px text-[10px] font-semibold tabular-nums leading-tight ${
                  isActive
                    ? "bg-primary/25 text-primary"
                    : "bg-muted/70 text-muted-foreground/80"
                }`}
              >
                {s.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
