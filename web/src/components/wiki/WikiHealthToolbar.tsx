import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  ClipboardCheck,
  Download,
  Eye,
  EyeOff,
  History,
  Loader2,
  Network,
  Pin,
  PinOff,
  RefreshCw,
  Scissors,
  Combine,
  Wrench,
  X,
  ListX,
} from "lucide-react";
import { useWikiLint } from "@/hooks/useWikiLint";
import { FailedBatchPanel } from "@/components/wiki/FailedBatchPanel";

interface Props {
  channelId: string;
  /** Called when the user clicks "Download" from the Tools menu. */
  onDownload?: () => void;
  /** Called when the user clicks "History" from the Tools menu. */
  onHistoryToggle?: () => void;
  /** Whether the version history panel is currently open. */
  historyOpen?: boolean;
  /** Number of stored versions — shown as a badge on the History item. */
  versionCount?: number;
  /** Re-plan folder boundaries (calls POST /wiki/refresh?mode=reorganize).
   *  Page contents may also refresh. */
  onReorganize?: () => void;
  /** Snapshot the current wiki to history then rebuild from scratch
   *  (calls POST /wiki/refresh?mode=rebuild). Destructive — UI shows
   *  a confirm step before firing. */
  onRebuild?: () => void;
  /** Whether a regeneration is currently running. */
  isRegenerating?: boolean;
  /** Number of failed extractions — Failures item is hidden when 0. */
  failureCount?: number;
  // ---- wiki-llm-native-redesign §5.15 — per-page curation -----------
  /** Slug of the page currently visible. When undefined, curation
   * items are hidden. */
  activeSlug?: string;
  /** Whether the active page is currently pinned. Drives the Pin
   * vs Unpin menu label. */
  activePagePinned?: boolean;
  /** Whether the active page is currently hidden. Drives the Hide
   * vs Show menu label. */
  activePageHidden?: boolean;
  /** Pin or unpin the active page. Argument toggles the state. */
  onPinToggle?: (pinned: boolean) => void;
  /** Hide or show the active page. Argument toggles the state. */
  onHideToggle?: (hidden: boolean) => void;
  /** Open the "split this page" flow. The toolbar collects the new
   * title; the parent supplies the fact-id selection. */
  onSplit?: (newTitle: string) => void;
  /** Open the "merge another page into this one" flow. */
  onMerge?: (sourceSlug: string) => void;
}

const SEVERITY_STYLES = {
  error: "border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 text-red-700 dark:text-red-300",
  warning: "border-amber-200 dark:border-amber-900/50 bg-amber-50 dark:bg-amber-950/30 text-amber-700 dark:text-amber-300",
  info: "border-sky-200 dark:border-sky-900/50 bg-sky-50 dark:bg-sky-950/30 text-sky-700 dark:text-sky-300",
} as const;

const TOOL_BTN =
  "flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-xs font-medium transition-colors hover:bg-muted text-foreground";

// Two-line tool item: short title on top, one-line description under.
// Used for Health check and Reorganize folders — operations whose
// names alone don't tell the operator what they do.
const TOOL_BTN_DESCRIBED =
  "flex w-full items-start gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors hover:bg-muted text-foreground";

/**
 * Wiki tools menu — passive utilities + destructive actions.
 *
 * The three primary wiki actions (Update / Reorganize / Rebuild) live
 * in the footer's "Update wiki" button, NOT here. This wrench-icon
 * menu is for read-only utilities + the Rebuild escape hatch.
 *
 * Layout (wrench icon → portal'd menu):
 *   ✅ Health check          — runs lint scan, opens findings panel
 *   🌳 Reorganize folders    — re-plan folder boundaries
 *   ─────────────
 *   🕘 History (N)
 *   📥 Download
 *   🔗 Graph
 *   ─────────────
 *   📌 Pin / Unpin           — only when activeSlug is set
 *   👁  Hide / Show          — only when activeSlug is set
 *   ✂  Split…                — only when activeSlug is set
 *   ⊕  Merge…                — only when activeSlug is set
 *   ─────────────
 *   ✕ Failures (N)           — only when failureCount > 0
 *   ⚠ Rebuild from scratch   — danger, shows confirm prompt
 */
export function WikiHealthToolbar({
  channelId,
  onDownload,
  onHistoryToggle,
  historyOpen = false,
  versionCount = 0,
  onReorganize,
  onRebuild,
  isRegenerating = false,
  failureCount,
  activeSlug,
  activePagePinned = false,
  activePageHidden = false,
  onPinToggle,
  onHideToggle,
  onSplit,
  onMerge,
}: Props) {
  const lint = useWikiLint(channelId);
  const navigate = useNavigate();
  const [reportOpen, setReportOpen] = useState(false);
  const [failuresOpen, setFailuresOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  // Curation prompt-modals (§5.15) — V1 are simple text-input dialogs.
  // The full split (fact-id picker) and merge (existing-page picker)
  // UIs are deferred to a follow-up; these capture the operator's
  // primary input and fire the parent callback.
  const [splitOpen, setSplitOpen] = useState(false);
  const [splitTitle, setSplitTitle] = useState("");
  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergeSlug, setMergeSlug] = useState("");
  const toolsRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  // Portal positioning: when the menu opens, measure the trigger's
  // viewport rect so we can render the menu in document.body without
  // being clipped by any ancestor with overflow:hidden. The dropdown
  // anchors to the LEFT side of the trigger and opens UPWARD into the
  // dark area between the sidebar and the main content.
  const [menuPos, setMenuPos] = useState<{ left: number; bottom: number } | null>(null);
  const showCurationItems = !!activeSlug;

  useLayoutEffect(() => {
    if (!toolsOpen) {
      setMenuPos(null);
      return;
    }
    const updatePos = () => {
      const el = triggerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      // Anchor menu's bottom to trigger's top (open UPWARD), and
      // align right edge of menu to right edge of trigger so it stays
      // close to the action.
      setMenuPos({
        left: rect.right + 8,
        bottom: window.innerHeight - rect.bottom,
      });
    };
    updatePos();
    window.addEventListener("resize", updatePos);
    window.addEventListener("scroll", updatePos, true);
    return () => {
      window.removeEventListener("resize", updatePos);
      window.removeEventListener("scroll", updatePos, true);
    };
  }, [toolsOpen]);

  // Close everything on Escape
  useEffect(() => {
    if (
      !reportOpen &&
      !failuresOpen &&
      !toolsOpen &&
      !splitOpen &&
      !mergeOpen
    )
      return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setReportOpen(false);
        setFailuresOpen(false);
        setToolsOpen(false);
        setConfirmRegenerate(false);
        setSplitOpen(false);
        setMergeOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [reportOpen, failuresOpen, toolsOpen, splitOpen, mergeOpen]);

  // Close Tools menu on outside click. The menu is portal'd into
  // document.body so a click on a menu item registers as outside
  // ``toolsRef``; we exempt anything carrying the
  // ``data-wiki-tools-menu`` marker so portal'd children don't trigger
  // the close before their own onClick handler fires.
  useEffect(() => {
    if (!toolsOpen) return;
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      if (!target) return;
      const insideTrigger =
        toolsRef.current && toolsRef.current.contains(target);
      const insideMenu = !!target.closest("[data-wiki-tools-menu]");
      if (!insideTrigger && !insideMenu) {
        setToolsOpen(false);
        setConfirmRegenerate(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [toolsOpen]);

  const findingsCount = lint.report?.findings.length ?? 0;
  const errorCount = lint.report?.findings.filter((f) => f.severity === "error").length ?? 0;
  const warnCount = lint.report?.findings.filter((f) => f.severity === "warning").length ?? 0;
  const lintBadge = findingsCount > 0 ? (errorCount + warnCount > 0 ? `${errorCount + warnCount}!` : findingsCount) : null;

  const showFailuresItem = failureCount === undefined ? true : failureCount > 0;

  return (
    <div className="flex flex-wrap items-center gap-2 justify-end">
      {/* Tools dropdown — square icon trigger so it sits cleanly next
          to the Regenerate button in the sidebar footer. Menu opens
          UPWARD because the trigger lives at the bottom of the sidebar
          and a downward menu would be clipped by the viewport. */}
      <div ref={toolsRef} className="relative">
        <button
          ref={triggerRef}
          type="button"
          aria-label="Wiki tools menu"
          aria-haspopup="true"
          aria-expanded={toolsOpen}
          onClick={() => {
            setToolsOpen((v) => !v);
            setConfirmRegenerate(false);
          }}
          className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-border/60 bg-background text-foreground hover:bg-muted hover:border-border transition-colors"
          title="Wiki tools"
        >
          <Wrench size={14} />
        </button>

        {toolsOpen && menuPos && createPortal(
          <div
            role="menu"
            aria-label="Wiki tools"
            data-wiki-tools-menu="true"
            style={{
              position: "fixed",
              left: `${menuPos.left}px`,
              bottom: `${menuPos.bottom}px`,
            }}
            className="z-[60] w-72 rounded-xl border border-border bg-popover p-1 shadow-2xl max-h-[70vh] overflow-y-auto"
          >
            {/* Health check — described item: explains what the lint
                scan checks so operators know whether they need it.
                Replaces both "Maintain Wiki" and the standalone Lint
                button: Update wiki (footer primary) covers maintenance,
                this surfaces rot signals (orphans, stale, duplicates,
                coherence) without modifying the wiki itself. */}
            <button
              role="menuitem"
              type="button"
              onClick={async () => {
                setToolsOpen(false);
                await lint.runLint();
                setReportOpen(true);
              }}
              disabled={lint.loading || !channelId}
              className={TOOL_BTN_DESCRIBED + " disabled:opacity-50 disabled:cursor-not-allowed"}
              aria-label="Health check — scan for orphan, stale, duplicate, and coherence issues"
            >
              <span className="mt-0.5 flex h-6 w-6 items-center justify-center rounded-md bg-amber-500/10 text-amber-600 dark:text-amber-400 shrink-0">
                {lint.loading ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <ClipboardCheck size={12} />
                )}
              </span>
              <span className="flex-1 min-w-0">
                <span className="flex items-center gap-1.5">
                  <span className="text-xs font-semibold text-foreground">
                    {lint.loading ? "Checking…" : "Health check"}
                  </span>
                  {lintBadge !== null && (
                    <span className="rounded-full bg-amber-100 dark:bg-amber-900/30 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700 dark:text-amber-300">
                      {lintBadge}
                    </span>
                  )}
                </span>
                <span className="block text-[11px] text-muted-foreground leading-snug mt-0.5">
                  Scan for orphan, stale, duplicate, and coherence issues.
                </span>
              </span>
            </button>

            {/* Reorganize folders — described item. Re-runs the
                structure planner against current pages. Page contents
                may also refresh because the builder regenerates pages
                in the same pass. Distinct from Update (which keeps the
                folder structure intact) and Rebuild (which wipes
                everything first). */}
            {onReorganize && (
              <button
                role="menuitem"
                type="button"
                onClick={() => {
                  setToolsOpen(false);
                  onReorganize();
                }}
                className={TOOL_BTN_DESCRIBED}
                aria-label="Reorganize folders — re-plan the wiki's folder hierarchy"
              >
                <span className="mt-0.5 flex h-6 w-6 items-center justify-center rounded-md bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 shrink-0">
                  <Network size={12} />
                </span>
                <span className="flex-1 min-w-0">
                  <span className="text-xs font-semibold text-foreground">
                    Reorganize folders
                  </span>
                  <span className="block text-[11px] text-muted-foreground leading-snug mt-0.5">
                    Re-plan folder boundaries. Page contents may also refresh.
                  </span>
                </span>
              </button>
            )}

            {/* Divider between described "primary actions" (Maintain /
                Lint / Restructure) and the secondary nav items
                (History / Download / Graph) below. */}
            <div className="my-1 h-px bg-border/60" />

            {/* History */}
            <button
              role="menuitem"
              type="button"
              onClick={() => {
                setToolsOpen(false);
                onHistoryToggle?.();
              }}
              disabled={versionCount === 0}
              className={TOOL_BTN + ` disabled:opacity-40 disabled:cursor-not-allowed ${historyOpen ? "bg-primary/10 text-primary hover:bg-primary/15" : ""}`}
              aria-label={`Version history${versionCount > 0 ? ` — ${versionCount} versions` : ""}`}
            >
              <History size={12} className="shrink-0" />
              <span className="flex-1">History</span>
              {versionCount > 0 && (
                <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground tabular-nums">
                  {versionCount}
                </span>
              )}
            </button>

            {/* Download */}
            <button
              role="menuitem"
              type="button"
              onClick={() => {
                setToolsOpen(false);
                onDownload?.();
              }}
              disabled={!channelId}
              className={TOOL_BTN + " disabled:opacity-40 disabled:cursor-not-allowed"}
              aria-label="Download wiki as Markdown"
            >
              <Download size={12} className="shrink-0" />
              <span className="flex-1">Download</span>
            </button>

            {/* Graph view — opens the wiki cross-link graph in-place
                inside the wiki tab via ?view=graph. The standalone
                /graph route was retired when the IA collapsed graphs
                into their parent tabs; this no longer switches tabs. */}
            <button
              role="menuitem"
              type="button"
              onClick={() => {
                setToolsOpen(false);
                navigate(`/channels/${channelId}/wiki?view=graph`);
              }}
              disabled={!channelId}
              className={TOOL_BTN + " disabled:opacity-40 disabled:cursor-not-allowed"}
              aria-label="Open the wiki cross-link graph view in this tab"
            >
              <Network size={12} className="shrink-0" />
              <span className="flex-1">Graph</span>
            </button>

            {/* Curation block (§5.15) — only when a page is active */}
            {showCurationItems && (
              <div className="my-1 h-px bg-border/60" />
            )}
            {showCurationItems && onPinToggle && (
              <button
                role="menuitem"
                type="button"
                onClick={() => {
                  setToolsOpen(false);
                  onPinToggle(!activePagePinned);
                }}
                className={TOOL_BTN}
                aria-label={
                  activePagePinned
                    ? "Unpin this page — allow the maintainer to restructure it"
                    : "Pin this page — keep its current layout"
                }
              >
                {activePagePinned ? (
                  <PinOff size={12} className="shrink-0" />
                ) : (
                  <Pin size={12} className="shrink-0" />
                )}
                <span className="flex-1">
                  {activePagePinned ? "Unpin" : "Pin"}
                </span>
              </button>
            )}
            {showCurationItems && onHideToggle && (
              <button
                role="menuitem"
                type="button"
                onClick={() => {
                  setToolsOpen(false);
                  onHideToggle(!activePageHidden);
                }}
                className={TOOL_BTN}
                aria-label={
                  activePageHidden
                    ? "Show this page in human nav"
                    : "Hide this page from human nav (still indexed for agents)"
                }
              >
                {activePageHidden ? (
                  <Eye size={12} className="shrink-0" />
                ) : (
                  <EyeOff size={12} className="shrink-0" />
                )}
                <span className="flex-1">
                  {activePageHidden ? "Show" : "Hide"}
                </span>
              </button>
            )}
            {showCurationItems && onSplit && (
              <button
                role="menuitem"
                type="button"
                onClick={() => {
                  setSplitTitle("");
                  setToolsOpen(false);
                  setSplitOpen(true);
                }}
                className={TOOL_BTN}
                aria-label="Split this page into two — extract a subset of facts to a new page"
              >
                <Scissors size={12} className="shrink-0" />
                <span className="flex-1">Split…</span>
              </button>
            )}
            {showCurationItems && onMerge && (
              <button
                role="menuitem"
                type="button"
                onClick={() => {
                  setMergeSlug("");
                  setToolsOpen(false);
                  setMergeOpen(true);
                }}
                className={TOOL_BTN}
                aria-label="Merge another page into this one"
              >
                <Combine size={12} className="shrink-0" />
                <span className="flex-1">Merge…</span>
              </button>
            )}

            {/* Divider — only when Failures or Rebuild is shown */}
            {(showFailuresItem || onRebuild) && (
              <div className="my-1 h-px bg-border/60" />
            )}

            {/* Failures — hidden when count is 0 */}
            {showFailuresItem && (
              <button
                role="menuitem"
                type="button"
                onClick={() => {
                  setToolsOpen(false);
                  setReportOpen(false);
                  setFailuresOpen((v) => !v);
                }}
                disabled={!channelId}
                className={TOOL_BTN + " disabled:opacity-40 disabled:cursor-not-allowed"}
                aria-label="View failed extractions"
              >
                <ListX size={12} className="shrink-0" />
                <span className="flex-1">Failures</span>
                {failureCount !== undefined && failureCount > 0 && (
                  <span className="rounded-full bg-red-100 dark:bg-red-900/30 px-1.5 py-0.5 text-[10px] font-semibold text-red-700 dark:text-red-300 tabular-nums">
                    {failureCount}
                  </span>
                )}
              </button>
            )}

            {/* Rebuild from scratch — danger. Snapshots the current
                wiki to history (so it's recoverable) then wipes the
                cache and regenerates everything from current memories
                with a fresh folder plan. Distinct from Reorganize
                (which keeps page IDs + curation flags) — Rebuild is
                the "reset to clean slate" action. */}
            {onRebuild && (
              confirmRegenerate ? (
                <div className="mt-1 rounded-md border border-red-200 dark:border-red-900/40 bg-red-50 dark:bg-red-950/20 p-2">
                  <p className="mb-2 text-[11px] text-red-700 dark:text-red-300 leading-snug">
                    This will snapshot the current wiki to history then
                    rebuild everything from scratch. Continue?
                  </p>
                  <div className="flex gap-1.5">
                    <button
                      type="button"
                      onClick={() => {
                        setConfirmRegenerate(false);
                        setToolsOpen(false);
                        onRebuild();
                      }}
                      className="flex-1 rounded px-2 py-1 text-[11px] font-medium bg-red-600 text-white hover:bg-red-700 transition-colors"
                      aria-label="Confirm rebuild wiki from scratch"
                    >
                      Rebuild
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmRegenerate(false)}
                      className="rounded px-2 py-1 text-[11px] font-medium border border-border hover:bg-muted transition-colors"
                      aria-label="Cancel rebuild"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  role="menuitem"
                  type="button"
                  onClick={() => setConfirmRegenerate(true)}
                  disabled={isRegenerating}
                  className={TOOL_BTN + " text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/20 disabled:opacity-40 disabled:cursor-not-allowed"}
                  aria-label="Rebuild wiki from scratch"
                >
                  {isRegenerating ? (
                    <Loader2 size={12} className="animate-spin shrink-0" />
                  ) : (
                    <RefreshCw size={12} className="shrink-0" />
                  )}
                  <span className="flex-1">Rebuild from scratch</span>
                </button>
              )
            )}
          </div>,
          document.body
        )}
      </div>

      {/* Lint findings panel — Sheet-style, slides from right */}
      {reportOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Lint findings"
          className="fixed right-0 top-0 z-40 h-full w-full max-w-sm md:w-96 bg-background border-l border-border shadow-2xl flex flex-col"
        >
          <header className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
            <h3 className="text-sm font-semibold">
              Lint findings
              {lint.report && (
                <span className="ml-1.5 text-xs font-normal text-muted-foreground">
                  ({findingsCount} across {lint.report.pages_scanned} pages)
                </span>
              )}
            </h3>
            <button
              onClick={() => setReportOpen(false)}
              className="p-0.5 rounded hover:bg-muted transition-colors"
              aria-label="Close findings"
            >
              <X size={14} />
            </button>
          </header>

          {/* Loading skeleton during lint scan */}
          {lint.loading && (
            <div
              role="status"
              aria-live="polite"
              className="flex-1 px-4 py-4 space-y-2"
            >
              <p className="text-xs text-muted-foreground mb-3">
                Scanning {lint.report?.pages_scanned ?? "…"} pages…
              </p>
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="h-12 rounded-lg bg-muted animate-pulse" />
              ))}
            </div>
          )}

          {!lint.loading && lint.error && (
            <div className="flex-1 px-4 py-6 space-y-3">
              <p className="text-sm text-rose-600 dark:text-rose-400">Lint failed: {lint.error}</p>
              <button
                type="button"
                onClick={async () => {
                  await lint.runLint();
                }}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md border border-border hover:bg-muted transition-colors"
                aria-label="Retry lint"
              >
                <RefreshCw size={12} />
                Retry
              </button>
            </div>
          )}

          {!lint.loading && !lint.error && lint.report && (
            <div className="flex-1 overflow-y-auto px-4 py-3">
              {findingsCount === 0 ? (
                <p className="text-xs text-muted-foreground py-3">
                  No issues — wiki is healthy.
                </p>
              ) : (
                <ul className="space-y-1.5">
                  {lint.report.findings.map((f, i) => (
                    <li
                      key={`${f.page_id}-${f.section_id || ""}-${i}`}
                      className={`rounded border px-2 py-1.5 text-[11px] ${SEVERITY_STYLES[f.severity]}`}
                    >
                      <div className="flex items-start gap-1.5">
                        {f.severity === "error" || f.severity === "warning" ? (
                          <AlertTriangle size={11} className="mt-0.5 shrink-0" />
                        ) : null}
                        <div className="min-w-0">
                          <p className="font-medium truncate">
                            {f.page_id}
                            {f.section_id && (
                              <span className="text-muted-foreground/70"> · {f.section_id}</span>
                            )}
                          </p>
                          <p className="leading-snug">{f.message}</p>
                          {f.suggested_action && (
                            <p className="mt-0.5 text-muted-foreground italic">
                              {f.suggested_action}
                            </p>
                          )}
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}

      {/* Failed extractions panel */}
      {failuresOpen && channelId && (
        <FailedBatchPanel
          channelId={channelId}
          onClose={() => setFailuresOpen(false)}
        />
      )}

      {/* §5.15 — Split prompt modal */}
      {splitOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-label="Split wiki page"
          onClick={() => setSplitOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-2xl border border-border bg-card p-6 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-lg font-semibold text-foreground">Split this page</h3>
            <p className="mt-2 text-sm text-muted-foreground">
              Choose a title for the new page. The maintainer will route a
              subset of this page's facts there on the next pass.
            </p>
            <input
              type="text"
              autoFocus
              value={splitTitle}
              onChange={(e) => setSplitTitle(e.target.value)}
              placeholder="e.g. Authentication — Session Policy"
              className="mt-4 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
              aria-label="New page title"
            />
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setSplitOpen(false)}
                className="rounded-md border border-border px-4 py-2 text-sm font-medium hover:bg-muted"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => {
                  const title = splitTitle.trim();
                  if (!title) return;
                  onSplit?.(title);
                  setSplitOpen(false);
                }}
                disabled={!splitTitle.trim()}
                className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                aria-label="Confirm split"
              >
                Split
              </button>
            </div>
          </div>
        </div>
      )}

      {/* §5.15 — Merge prompt modal */}
      {mergeOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-label="Merge wiki page"
          onClick={() => setMergeOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-2xl border border-border bg-card p-6 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-lg font-semibold text-foreground">Merge into this page</h3>
            <p className="mt-2 text-sm text-muted-foreground">
              Enter the slug of the page that should be merged INTO the
              current one. The merged-from page will be hidden from human
              nav and future facts will route here.
            </p>
            <input
              type="text"
              autoFocus
              value={mergeSlug}
              onChange={(e) => setMergeSlug(e.target.value)}
              placeholder="e.g. topic-auth-old"
              className="mt-4 w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono"
              aria-label="Source slug"
            />
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setMergeOpen(false)}
                className="rounded-md border border-border px-4 py-2 text-sm font-medium hover:bg-muted"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => {
                  const slug = mergeSlug.trim();
                  if (!slug) return;
                  onMerge?.(slug);
                  setMergeOpen(false);
                }}
                disabled={!mergeSlug.trim()}
                className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                aria-label="Confirm merge"
              >
                Merge
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
