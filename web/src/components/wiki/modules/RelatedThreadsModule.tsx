/** Related threads module — capped 5-item list of related topics
 *  with a "why related" reason per link. Phase 7+ may add hover
 *  preview of the related topic's TL;DR. */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function RelatedThreadsModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
