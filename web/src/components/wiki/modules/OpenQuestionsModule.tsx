/** Open questions module — bullet list with raised-on dates.
 *  Phase 7+ may add per-question status (resolved/still-open) badges. */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function OpenQuestionsModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
