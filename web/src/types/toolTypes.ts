export type ToolCategory = "wiki" | "memory" | "graph" | "external";

export interface ToolDescriptor {
  name: string;
  category: ToolCategory;
  description: string;
}
