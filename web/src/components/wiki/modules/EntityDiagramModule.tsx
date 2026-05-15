/** Entity diagram module — mermaid `graph TD` of entity relationships.
 *  Reuses the existing mermaid renderer in WikiMarkdown. */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function EntityDiagramModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
