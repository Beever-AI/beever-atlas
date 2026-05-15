/** Quote highlights module — verbatim quotes with attribution.
 *  Phase 7+ may add per-quote "open thread" links and author chips. */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function QuoteHighlightsModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
