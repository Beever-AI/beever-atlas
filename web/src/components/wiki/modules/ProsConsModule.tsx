/** Pros & Cons module — two-column trade-off table.
 *  Phase 7+ may render as side-by-side cards (green pros / red cons). */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function ProsConsModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
