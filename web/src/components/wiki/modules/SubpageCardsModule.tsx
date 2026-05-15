/** Subpage cards module — children TOC for parent topics that own
 *  sub-pages. Phase 7+ may render as the rich card grid used by
 *  FolderPage instead of the bullet list the compiler emits. */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function SubpageCardsModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
