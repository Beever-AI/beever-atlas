/** Comparison matrix module — N alternatives × M criteria GFM table.
 *  Phase 7+ may add sticky-header behavior for wide comparisons. */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function ComparisonMatrixModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
