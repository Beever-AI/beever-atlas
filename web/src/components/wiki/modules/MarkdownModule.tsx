/**
 * Shared base renderer for any module whose data is pre-rendered
 * markdown (the compiler-rendered modules: key_facts, decision_log,
 * timeline, comparison_matrix, pros_cons, quote_highlights,
 * flow_chart, entity_diagram, open_questions, subpage_cards,
 * related_threads).
 *
 * Renders an H2 heading from the module's catalog label, then the
 * markdown via WikiMarkdown. The H2 contributes to the right-side
 * TOC; everything inside the wrapper is `data-toc-skip` so module
 * internals don't pollute the TOC across pages with different
 * module mixes.
 */

import { WikiMarkdown } from "../WikiMarkdown";
import type { ModuleProps } from "./ModuleRenderer";

export function MarkdownModule({ module, citations, onNavigate }: ModuleProps) {
  const data = module.data ?? {};
  const label =
    typeof data.label === "string" && data.label
      ? data.label
      : module.id.replace(/_/g, " ");
  const markdown =
    typeof data.markdown === "string" ? data.markdown : "";

  if (!markdown) {
    // Module was picked but rendered empty — skip silently rather
    // than show a stub heading. The validator's job is to reject
    // empty-data modules at plan time; this is the safety net.
    return null;
  }

  return (
    <section className="mt-8" id={`module-${module.anchor}`}>
      <h2 className="text-lg font-semibold text-foreground capitalize mb-3">
        {label}
      </h2>
      <div data-toc-skip>
        <WikiMarkdown
          content={markdown}
          citations={citations}
          onNavigate={onNavigate}
        />
      </div>
    </section>
  );
}
