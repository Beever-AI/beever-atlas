/**
 * Adaptive page module dispatcher.
 *
 * Reads `page.modules[]` and routes each entry to its dedicated React
 * component. The switch on `module.id` is the single source of truth
 * for what module IDs are recognised — adding a new module type
 * requires (a) adding the catalog entry on the backend and (b)
 * adding both a component file and a switch case here.
 *
 * Compiler-rendered modules (markdown-emitting: key_facts,
 * decision_log, timeline, comparison_matrix, pros_cons,
 * quote_highlights, flow_chart, entity_diagram, open_questions,
 * subpage_cards, related_threads) all use the shared `MarkdownModule`
 * wrapper which renders the pre-rendered markdown via WikiMarkdown
 * with citation awareness.
 *
 * Frontend-only modules (media_*, link_card, pdf_preview,
 * video_embed) get specialized components in Phase 7 — for v1 they
 * fall through to MarkdownModule (which renders the markdown the
 * compiler produced for them, if any).
 */

import type { WikiCitation, WikiPageModule } from "@/lib/types";
import { HeroSummaryModule } from "./HeroSummaryModule";
import { KeyFactsModule } from "./KeyFactsModule";
import { DecisionLogModule } from "./DecisionLogModule";
import { TimelineModule } from "./TimelineModule";
import { ComparisonMatrixModule } from "./ComparisonMatrixModule";
import { ProsConsModule } from "./ProsConsModule";
import { QuoteHighlightsModule } from "./QuoteHighlightsModule";
import { FlowChartModule } from "./FlowChartModule";
import { EntityDiagramModule } from "./EntityDiagramModule";
import { OpenQuestionsModule } from "./OpenQuestionsModule";
import { SubpageCardsModule } from "./SubpageCardsModule";
import { RelatedThreadsModule } from "./RelatedThreadsModule";
import { MediaHeroModule } from "./MediaHeroModule";
import { MediaInlineModule } from "./MediaInlineModule";
import { MediaGalleryModule } from "./MediaGalleryModule";
import { LinkCardModule } from "./LinkCardModule";
import { PdfPreviewModule } from "./PdfPreviewModule";
import { VideoEmbedModule } from "./VideoEmbedModule";
import { ProvenanceDrawerModule } from "./ProvenanceDrawerModule";
import { AcronymLegendModule } from "./AcronymLegendModule";
import { StatStripModule } from "./StatStripModule";
import { DecisionBannerModule } from "./DecisionBannerModule";
import { TensionCalloutModule } from "./TensionCalloutModule";
import { FolderStatsModule } from "./FolderStatsModule";
import { TopContributorsModule } from "./TopContributorsModule";
import { CrossCuttingDecisionsModule } from "./CrossCuttingDecisionsModule";
import { NarrativeArticleModule } from "./NarrativeArticleModule";

export interface ModuleProps {
  module: WikiPageModule;
  citations: WikiCitation[];
  onNavigate?: (pageId: string) => void;
}

interface ModuleRendererProps {
  modules: WikiPageModule[];
  citations: WikiCitation[];
  onNavigate?: (pageId: string) => void;
}

/**
 * Pull the page-level glossary out of the ``acronym_legend`` module's
 * ``items`` list so KeyFactsModule (and any other body renderer that
 * wants to surface inline definitions) can highlight matched terms
 * without re-fetching anything. The acronym_legend builder already
 * filters down to terms ACTUALLY mentioned on this page, so this map
 * is page-scoped — no risk of inflating definitions onto unrelated
 * acronyms.
 */
function extractGlossary(
  modules: WikiPageModule[],
): Record<string, string> | undefined {
  const legend = modules.find((m) => m.id === "acronym_legend");
  if (!legend) return undefined;
  const items = (legend.data as { items?: unknown } | undefined)?.items;
  if (!Array.isArray(items)) return undefined;
  const out: Record<string, string> = {};
  for (const raw of items) {
    if (!raw || typeof raw !== "object") continue;
    const item = raw as { term?: unknown; definition?: unknown };
    const term = typeof item.term === "string" ? item.term.trim() : "";
    const def =
      typeof item.definition === "string" ? item.definition.trim() : "";
    if (term && def) out[term] = def;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

// Module IDs that belong in the bottom-of-page "reading aids" footer.
// They're reference helpers (acronym definitions + source-message
// drawer), not core spine content, so we visually demote them by
// wrapping them in a single ``<aside>`` block with subtle top border
// and lighter background tint.
const READING_AIDS_IDS = new Set(["acronym_legend", "provenance_drawer"]);

/** Reading-aid renderers (``AcronymLegendModule``, ``ProvenanceDrawerModule``)
 *  return ``null`` when their data payload is empty. The wrapping
 *  ``<aside data-testid="wiki-page-footer">`` block in
 *  ``ModuleRenderer`` would still render its "Reading aids" header in
 *  that case, leaving an orphan label with no body — confusing for the
 *  user. This predicate filters empty modules out at the parent so the
 *  whole aside is suppressed when both children would render null.
 *
 *  Keep this in sync with the inner-renderer empty-checks:
 *  - ``AcronymLegendModule.tsx`` returns null when ``data.items`` is empty.
 *  - ``ProvenanceDrawerModule.tsx`` returns null when ``data.messages``
 *    is empty.
 */
function hasReadingAidContent(module: WikiPageModule): boolean {
  const data = (module.data ?? {}) as Record<string, unknown>;
  if (module.id === "acronym_legend") {
    return Array.isArray(data.items) && data.items.length > 0;
  }
  if (module.id === "provenance_drawer") {
    return Array.isArray(data.messages) && data.messages.length > 0;
  }
  // Defensive default — any future reading-aid module passes through
  // until a specific predicate is added here.
  return true;
}

// The narrative_article module is the new spotlight (see
// ``wiki-narrative-articles`` change) — when present it renders FIRST
// in its own reading column, and the existing 26 modules below get
// demoted into a collapsible "Reference & Evidence" appendix. When
// absent (flag OFF, fallback fired, pre-narrative page), today's
// module-only behavior is preserved.
const NARRATIVE_ARTICLE_ID = "narrative_article";

function renderModule(
  module: WikiPageModule,
  citations: WikiCitation[],
  onNavigate: ((pageId: string) => void) | undefined,
  glossary: Record<string, string> | undefined,
) {
  // For modules that support inline glossary highlighting, inject
  // the page-scoped glossary into ``module.data`` so the renderer
  // can wrap matched acronyms in title-tooltip spans. Other
  // modules ignore the extra field.
  const moduleForRender =
    glossary && module.id === "key_facts"
      ? {
          ...module,
          data: { ...(module.data ?? {}), glossary },
        }
      : module;
  const props: ModuleProps = {
    module: moduleForRender,
    citations,
    onNavigate,
  };
  return dispatchModule(moduleForRender, props);
}

export function ModuleRenderer({
  modules,
  citations,
  onNavigate,
}: ModuleRendererProps) {
  const glossary = extractGlossary(modules);
  // Split modules into three buckets:
  //   1. narrative_article — the new spotlight, renders FIRST when present
  //   2. spine modules — existing 26 modules (key_facts, decision_log, ...)
  //   3. reading aids — acronym_legend + provenance_drawer
  //
  // When a narrative_article is present, the spine modules render
  // inside a collapsible "Reference & Evidence" appendix below the
  // article. When absent, today's module-only layout is preserved.
  const narrativeModule = modules.find((m) => m.id === NARRATIVE_ARTICLE_ID);
  const spineModules = modules.filter(
    (m) => !READING_AIDS_IDS.has(m.id) && m.id !== NARRATIVE_ARTICLE_ID,
  );
  const readingAidsModules = modules
    .filter((m) => READING_AIDS_IDS.has(m.id))
    .filter((m) => hasReadingAidContent(m));
  // Keep the canonical reading-aids order: acronym_legend first
  // (definitions help understand the page body), then provenance
  // drawer (sources you might want to follow up on).
  readingAidsModules.sort((a, b) => {
    if (a.id === b.id) return 0;
    if (a.id === "acronym_legend") return -1;
    if (b.id === "acronym_legend") return 1;
    return 0;
  });

  const hasNarrative = narrativeModule !== undefined;

  return (
    <>
      {hasNarrative &&
        renderModule(narrativeModule, citations, onNavigate, glossary)}

      {hasNarrative ? (
        spineModules.length > 0 && (
          <details
            data-testid="wiki-reference-evidence"
            className="mt-12 border-t border-border pt-8"
            open
          >
            <summary className="cursor-pointer font-medium text-sm text-muted-foreground uppercase tracking-wide select-none">
              Reference &amp; Evidence ({spineModules.length} supporting{" "}
              {spineModules.length === 1 ? "module" : "modules"})
            </summary>
            <div className="mt-6 space-y-8">
              {spineModules.map((module) =>
                renderModule(module, citations, onNavigate, glossary),
              )}
            </div>
          </details>
        )
      ) : (
        spineModules.map((module) =>
          renderModule(module, citations, onNavigate, glossary),
        )
      )}

      {readingAidsModules.length > 0 && (
        <aside
          className="wiki-page-footer mt-10 pt-6 border-t border-border/60 bg-muted/5 -mx-2 px-2 rounded-b-lg text-[13px] text-muted-foreground"
          data-testid="wiki-page-footer"
          data-toc-skip
        >
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/70 mb-2 inline-flex items-center gap-1.5">
            <span aria-hidden="true">📚</span>
            <span>Reading aids</span>
          </h2>
          {readingAidsModules.map((module) =>
            renderModule(module, citations, onNavigate, glossary),
          )}
        </aside>
      )}
    </>
  );
}

function dispatchModule(module: WikiPageModule, props: ModuleProps) {
  switch (module.id) {
    case "narrative_article":
      return <NarrativeArticleModule key={module.anchor} {...props} />;
    case "hero_summary":
      return <HeroSummaryModule key={module.anchor} {...props} />;
    case "key_facts":
      return <KeyFactsModule key={module.anchor} {...props} />;
    case "decision_log":
      return <DecisionLogModule key={module.anchor} {...props} />;
    case "timeline":
      return <TimelineModule key={module.anchor} {...props} />;
    case "comparison_matrix":
      return <ComparisonMatrixModule key={module.anchor} {...props} />;
    case "pros_cons":
      return <ProsConsModule key={module.anchor} {...props} />;
    case "quote_highlights":
      return <QuoteHighlightsModule key={module.anchor} {...props} />;
    case "flow_chart":
      return <FlowChartModule key={module.anchor} {...props} />;
    case "entity_diagram":
      return <EntityDiagramModule key={module.anchor} {...props} />;
    case "open_questions":
      return <OpenQuestionsModule key={module.anchor} {...props} />;
    case "subpage_cards":
      return <SubpageCardsModule key={module.anchor} {...props} />;
    case "related_threads":
      return <RelatedThreadsModule key={module.anchor} {...props} />;
    // Media modules — frontend-only renderers consume the
    // structured ``module.data`` payload (URLs, captions, items
    // list) the orchestrator's ``_extract_media_for_module``
    // populated. These bypass the markdown path entirely.
    case "media_hero":
      return <MediaHeroModule key={module.anchor} {...props} />;
    case "media_inline":
      return <MediaInlineModule key={module.anchor} {...props} />;
    case "media_gallery":
      return <MediaGalleryModule key={module.anchor} {...props} />;
    case "link_card":
      return <LinkCardModule key={module.anchor} {...props} />;
    case "pdf_preview":
      return <PdfPreviewModule key={module.anchor} {...props} />;
    case "video_embed":
      return <VideoEmbedModule key={module.anchor} {...props} />;
    // Provenance + reading-aid modules — frontend-only renderers
    // consume the structured ``module.data`` payload built by the
    // matching Python builder (provenance_drawer / acronym_legend
    // / stat_strip). All 3 are content-fullness modules added to
    // give human readers + LLM agents richer access to the same
    // source data.
    case "stat_strip":
      return <StatStripModule key={module.anchor} {...props} />;
    case "decision_banner":
      return <DecisionBannerModule key={module.anchor} {...props} />;
    case "tension_callout":
      return <TensionCalloutModule key={module.anchor} {...props} />;
    case "acronym_legend":
      return <AcronymLegendModule key={module.anchor} {...props} />;
    case "provenance_drawer":
      return <ProvenanceDrawerModule key={module.anchor} {...props} />;
    // Folder-archetype dashboard modules — replace the legacy
    // "Themes & threads" prose blob with at-a-glance modules.
    // Only fire on folder index pages (planner predicates gate
    // archetype == 'folder').
    case "folder_stats":
      return <FolderStatsModule key={module.anchor} {...props} />;
    case "top_contributors":
      return <TopContributorsModule key={module.anchor} {...props} />;
    case "cross_cutting_decisions":
      return <CrossCuttingDecisionsModule key={module.anchor} {...props} />;
    default:
      // Unknown module ID — silently drop. The backend's
      // validator should have rejected it before persistence;
      // surfacing a fallback would mask a backend bug.
      return null;
  }
}
