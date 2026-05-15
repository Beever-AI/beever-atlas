/** Flow chart module — mermaid `graph LR` for process pipelines.
 *  Reuses the existing mermaid renderer in WikiMarkdown. */
import { MarkdownModule } from "./MarkdownModule";
import type { ModuleProps } from "./ModuleRenderer";

export function FlowChartModule(props: ModuleProps) {
  return <MarkdownModule {...props} />;
}
