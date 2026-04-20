export type ToolCategory = "wiki" | "memory" | "graph" | "external" | "orchestration";

export interface ToolDescriptor {
  name: string;
  category: ToolCategory;
  description: string;
}
