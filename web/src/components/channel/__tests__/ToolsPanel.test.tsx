import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToolsPanel } from "../ToolsPanel";
import type { ToolDescriptor } from "@/types/toolTypes";

const makeDescriptors = (): ToolDescriptor[] => [
  { name: "wiki_search", category: "wiki", description: "Search wiki pages" },
  { name: "wiki_fetch", category: "wiki", description: "Fetch a wiki page" },
  { name: "wiki_list", category: "wiki", description: "List wiki pages" },
  { name: "memory_recall", category: "memory", description: "Recall from memory" },
  { name: "memory_store", category: "memory", description: "Store to memory" },
  { name: "graph_query", category: "graph", description: "Query the graph" },
  { name: "graph_traverse", category: "graph", description: "Traverse graph edges" },
  { name: "graph_lookup", category: "graph", description: "Look up a graph node" },
  { name: "web_search", category: "external", description: "Search the web" },
  { name: "web_fetch", category: "external", description: "Fetch a URL" },
];

describe("ToolsPanel", () => {
  it("renders collapsed by default showing enabled count", () => {
    const descriptors = makeDescriptors();
    render(
      <ToolsPanel
        descriptors={descriptors}
        disabledTools={["wiki_search", "memory_recall"]}
        onToggle={vi.fn()}
      />,
    );

    // Header shows correct enabled count
    expect(screen.getByText("Tools (8/10 enabled)")).toBeInTheDocument();

    // Category labels NOT visible when collapsed
    expect(screen.queryByText("Wiki")).not.toBeInTheDocument();
  });

  it("renders all 10 tools grouped by category when expanded", async () => {
    const user = userEvent.setup();
    const descriptors = makeDescriptors();
    render(
      <ToolsPanel
        descriptors={descriptors}
        disabledTools={[]}
        onToggle={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /tools/i }));

    // Category section headings
    expect(screen.getByText("Wiki")).toBeInTheDocument();
    expect(screen.getByText("Memory")).toBeInTheDocument();
    expect(screen.getByText("Graph")).toBeInTheDocument();
    expect(screen.getByText("External")).toBeInTheDocument();

    // All 10 tool names present
    for (const d of descriptors) {
      expect(screen.getByText(d.name)).toBeInTheDocument();
    }
  });

  it("calls onToggle with the tool name when a toggle is clicked", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    const descriptors = makeDescriptors();

    render(
      <ToolsPanel
        descriptors={descriptors}
        disabledTools={[]}
        onToggle={onToggle}
      />,
    );

    // Expand panel
    await user.click(screen.getByRole("button", { name: /tools/i }));

    // Click the toggle for "wiki_search"
    const toggle = screen.getByRole("switch", { name: /wiki_search/i });
    await user.click(toggle);

    expect(onToggle).toHaveBeenCalledOnce();
    expect(onToggle).toHaveBeenCalledWith("wiki_search");
  });

  it("reflects disabled state via aria-pressed", async () => {
    const user = userEvent.setup();
    const descriptors = makeDescriptors();

    render(
      <ToolsPanel
        descriptors={descriptors}
        disabledTools={["graph_query"]}
        onToggle={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /tools/i }));

    // graph_query is disabled → aria-pressed=false
    const disabledToggle = screen.getByRole("switch", { name: /graph_query/i });
    expect(disabledToggle).toHaveAttribute("aria-pressed", "false");

    // wiki_search is enabled → aria-pressed=true
    const enabledToggle = screen.getByRole("switch", { name: /wiki_search/i });
    expect(enabledToggle).toHaveAttribute("aria-pressed", "true");
  });
});
