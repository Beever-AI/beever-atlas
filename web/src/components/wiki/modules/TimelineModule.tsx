/** Timeline module — ordered events with date prefixes.
 *  Phase 7+ may render as a vertical visual timeline with date ticks. */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function TimelineModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
