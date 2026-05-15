import { useMemo } from "react";
import { ArrowRight, Clock, FileText, FolderOpen } from "lucide-react";
import { WikiMarkdown } from "./WikiMarkdown";
import { CitationPanel } from "./CitationPanel";
import { ModuleRenderer } from "./modules";
import type { WikiPage, WikiPageRef } from "@/lib/types";
import { wikiT } from "@/lib/wikiI18n";

interface FolderPageProps {
  page: WikiPage;
  onNavigate: (pageId: string) => void;
  lang?: string;
}

// ── Helpers ────────────────────────────────────────────────────────────

function relativeAgo(iso: string | undefined | null): string {
  if (!iso) return "";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "";
  const diffMs = Date.now() - ts;
  const min = Math.round(diffMs / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day}d ago`;
  const mon = Math.round(day / 30);
  return `${mon}mo ago`;
}

/**
 * Split a folder page's LLM body into three parts:
 *   intro      — text before the first /wiki/ bullet
 *   bulletMap  — parsed bullets keyed by slug → summary
 *   outro      — text after the last /wiki/ bullet
 *
 * The wiki-folder index template emits an intro, a deterministic
 * children-TOC bullet block (rendered server-side from the marker), and
 * a closing synthesis paragraph. We render the bullets as rich cards
 * instead, so we extract the per-child summary text from each bullet
 * and drop the bullet block from the markdown body.
 */
function parseFolderBody(content: string): {
  intro: string;
  bulletMap: Map<string, string>;
  outro: string;
} {
  const lines = content.split("\n");
  const bulletRegex = /^\s*-\s*\[(.+?)\]\(\/wiki\/([^)]+)\)\s*(?:[—–-]\s*(.*))?$/;

  let firstBulletIdx = -1;
  let lastBulletIdx = -1;
  const bulletMap = new Map<string, string>();

  lines.forEach((line, idx) => {
    const m = line.match(bulletRegex);
    if (m) {
      if (firstBulletIdx === -1) firstBulletIdx = idx;
      lastBulletIdx = idx;
      const slug = m[2].trim();
      const summary = (m[3] || "").trim();
      if (slug && summary && !bulletMap.has(slug)) {
        bulletMap.set(slug, summary);
      }
    }
  });

  if (firstBulletIdx === -1) {
    // No /wiki/ bullets found — render the whole body as intro and let
    // the cards stand on their own without per-child summaries.
    return { intro: content.trim(), bulletMap, outro: "" };
  }

  const intro = lines.slice(0, firstBulletIdx).join("\n").trim();
  const outro = lines.slice(lastBulletIdx + 1).join("\n").trim();
  return { intro, bulletMap, outro };
}

// ── Header chips (same shape as OverviewPage) ─────────────────────────

interface MetaChipProps {
  icon: React.ReactNode;
  label: string;
  value: number | string;
}
function MetaChip({ icon, label, value }: MetaChipProps) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-muted/40 px-2 py-1 text-[11px] text-muted-foreground"
      title={label}
    >
      <span className="text-muted-foreground/70">{icon}</span>
      <span className="font-semibold text-foreground tabular-nums">{value}</span>
      <span>{label}</span>
    </span>
  );
}

// ── Child card ────────────────────────────────────────────────────────

interface ChildCardProps {
  child: WikiPageRef;
  summary: string;
  onNavigate: (pageId: string) => void;
  lang?: string;
}
function ChildCard({ child, summary, onNavigate, lang }: ChildCardProps) {
  return (
    <button
      type="button"
      onClick={() => onNavigate(child.id)}
      className="group text-left rounded-xl border border-border bg-card p-4 hover:border-primary/40 hover:shadow-sm transition-all duration-150 flex flex-col h-full"
    >
      <div className="flex items-start gap-2.5 mb-2">
        <span className="shrink-0 mt-0.5 flex h-6 w-6 items-center justify-center rounded-md bg-muted/80 text-muted-foreground/80 group-hover:bg-primary/10 group-hover:text-primary transition-colors">
          <FileText size={12} />
        </span>
        <div className="flex-1 min-w-0">
          {child.section_number && (
            <span className="block text-[10.5px] font-mono text-muted-foreground/60 tabular-nums mb-0.5">
              {child.section_number}
            </span>
          )}
          <h3 className="text-sm font-semibold text-foreground leading-snug group-hover:text-primary transition-colors line-clamp-2">
            {child.title}
          </h3>
        </div>
      </div>
      {summary && (
        <p className="text-[12px] text-muted-foreground leading-relaxed line-clamp-3 flex-1 mb-3">
          {summary}
        </p>
      )}
      <div className="mt-auto flex items-center justify-between text-[11px] text-muted-foreground/70">
        <span className="tabular-nums">
          {child.memory_count > 0
            ? wikiT(lang, "memoriesSuffix", { n: child.memory_count })
            : "—"}
        </span>
        <span className="inline-flex items-center gap-1 text-primary/70 group-hover:text-primary transition-colors">
          Open <ArrowRight size={11} />
        </span>
      </div>
    </button>
  );
}

// ── Folder page ───────────────────────────────────────────────────────

export function FolderPage({ page, onNavigate, lang }: FolderPageProps) {
  const content = page.content.replace(/^#\s+[^\n]+\n*/, "");
  // The modular folder pipeline persists structured modules on
  // ``page.modules`` (hero_summary + subpage_cards + folder_stats +
  // top_contributors + cross_cutting_decisions + provenance_drawer).
  // When present, the React dispatcher renders the dashboard directly
  // and the legacy "Themes & threads" prose path is bypassed entirely.
  const hasModularPlan = (page.modules ?? []).length > 0;

  const { intro, bulletMap, outro } = useMemo(
    () => (hasModularPlan ? { intro: "", bulletMap: new Map<string, string>(), outro: "" } : parseFolderBody(content)),
    [content, hasModularPlan],
  );

  const childCount = page.children?.length ?? 0;
  const totalChildMemories = (page.children ?? []).reduce(
    (s, c) => s + (c.memory_count ?? 0),
    0,
  );
  const freshness = relativeAgo(page.last_updated);

  return (
    <div>
      {/* ── Header ──────────────────────────────────────────────────── */}
      <div className="flex items-start gap-3">
        <span className="shrink-0 mt-1 flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <FolderOpen size={18} />
        </span>
        <div className="flex-1 min-w-0">
          <h1 className="text-2xl font-bold text-foreground">{page.title}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <MetaChip
              icon={<FolderOpen size={11} />}
              label={childCount === 1 ? "page" : "pages"}
              value={childCount}
            />
            {totalChildMemories > 0 && (
              <MetaChip
                icon={<FileText size={11} />}
                label="memories"
                value={totalChildMemories}
              />
            )}
            {freshness && (
              <MetaChip icon={<Clock size={11} />} label={freshness} value="updated" />
            )}
          </div>
        </div>
      </div>

      {hasModularPlan ? (
        // ── Modular dashboard path ─────────────────────────────────
        // The module dispatcher renders the folder dashboard:
        // hero_summary (TL;DR + summary), subpage_cards (rendered as
        // child cards below), folder_stats (4-card big-number strip),
        // top_contributors (chip strip), cross_cutting_decisions
        // (decision list), open_questions, provenance_drawer.
        <>
          <div className="mt-5">
            <ModuleRenderer
              modules={(page.modules ?? []).filter(
                (m) => m.id !== "subpage_cards",
              )}
              citations={page.citations}
              onNavigate={onNavigate}
            />
          </div>
          {childCount > 0 && (
            <div className="mt-6" data-toc-skip>
              <h2 className="text-lg font-semibold text-foreground mb-3 flex items-center gap-2">
                Pages in this folder
                <span className="text-[11px] font-normal text-muted-foreground">
                  ({childCount})
                </span>
              </h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-2 gap-4">
                {(page.children ?? []).map((child) => (
                  <ChildCard
                    key={child.id}
                    child={child}
                    summary=""
                    onNavigate={onNavigate}
                    lang={lang}
                  />
                ))}
              </div>
            </div>
          )}
        </>
      ) : (
        // ── Legacy prose path (kept for non-modular fallback pages) ─
        <>
          {intro && (
            <div className="mt-5 max-w-none">
              <WikiMarkdown content={intro} citations={page.citations} onNavigate={onNavigate} />
            </div>
          )}
          {childCount > 0 && (
            <div className="mt-6" data-toc-skip>
              <h2 className="text-lg font-semibold text-foreground mb-3 flex items-center gap-2">
                Pages in this folder
                <span className="text-[11px] font-normal text-muted-foreground">
                  ({childCount})
                </span>
              </h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-2 gap-4">
                {(page.children ?? []).map((child) => (
                  <ChildCard
                    key={child.id}
                    child={child}
                    summary={bulletMap.get(child.slug) ?? ""}
                    onNavigate={onNavigate}
                    lang={lang}
                  />
                ))}
              </div>
            </div>
          )}
          {outro && (
            <div className="mt-8 max-w-none">
              <h2 className="text-lg font-semibold text-foreground mb-3">Themes & threads</h2>
              <WikiMarkdown content={outro} citations={page.citations} onNavigate={onNavigate} />
            </div>
          )}
        </>
      )}

      <CitationPanel citations={page.citations} />
    </div>
  );
}
