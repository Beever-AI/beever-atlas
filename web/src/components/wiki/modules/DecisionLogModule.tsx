/** Decision log module — GFM table with status badges.
 *  Phase 7+ may add per-row status filtering. */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function DecisionLogModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
