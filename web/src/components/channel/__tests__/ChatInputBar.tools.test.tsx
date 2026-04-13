import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatInputBar } from "../ChatInputBar";
import type { ToolDescriptor } from "@/types/toolTypes";

const DESCRIPTORS: ToolDescriptor[] = [
  { name: "wiki_search", category: "wiki", description: "Search wiki pages" },
  { name: "memory_recall", category: "memory", description: "Recall from memory" },
  { name: "graph_query", category: "graph", description: "Query the graph" },
  { name: "web_search", category: "external", description: "Search the web" },
];

function renderBar(overrides: Partial<Parameters<typeof ChatInputBar>[0]> = {}) {
  const onSubmit = vi.fn();
  const onModeChange = vi.fn();
  render(
    <ChatInputBar
      onSubmit={onSubmit}
      isStreaming={false}
      mode="deep"
      onModeChange={onModeChange}
      toolDescriptors={DESCRIPTORS}
      disabledTools={[]}
      onToggleTool={vi.fn()}
      {...overrides}
    />,
  );
  return { onSubmit, onModeChange };
}

describe("ChatInputBar — Tools popover", () => {
  it("renders the Tools button when descriptors are provided", () => {
    renderBar();
    expect(screen.getByRole("button", { name: /tools/i })).toBeInTheDocument();
  });

  it("does NOT render the Tools button when toolDescriptors is empty/undefined", () => {
    renderBar({ toolDescriptors: undefined });
    expect(screen.queryByRole("button", { name: /tools/i })).not.toBeInTheDocument();
  });

  it("shows enabled/total count on the Tools button", () => {
    renderBar({ disabledTools: ["wiki_search"] });
    // 3 out of 4 enabled
    expect(screen.getByText("(3/4)")).toBeInTheDocument();
  });

  it("opens popover on click, showing all 4 tools grouped by category", async () => {
    const user = userEvent.setup();
    renderBar();

    await user.click(screen.getByRole("button", { name: /tools/i }));

    // Category headers
    expect(screen.getByText("Wiki")).toBeInTheDocument();
    expect(screen.getByText("Memory")).toBeInTheDocument();
    expect(screen.getByText("Graph")).toBeInTheDocument();
    expect(screen.getByText("External")).toBeInTheDocument();

    // All tool names
    for (const d of DESCRIPTORS) {
      expect(screen.getByText(d.name)).toBeInTheDocument();
    }
  });

  it("calls onToggleTool with the tool name when a toggle is clicked", async () => {
    const user = userEvent.setup();
    const onToggleTool = vi.fn();
    renderBar({ onToggleTool });

    // Open popover
    await user.click(screen.getByRole("button", { name: /tools/i }));

    // Click the toggle for "graph_query"
    const toggle = screen.getByRole("switch", { name: /graph_query/i });
    await user.click(toggle);

    expect(onToggleTool).toHaveBeenCalledOnce();
    expect(onToggleTool).toHaveBeenCalledWith("graph_query");
  });

  it("closes popover when clicking outside (backdrop)", async () => {
    const user = userEvent.setup();
    renderBar();

    // Open
    await user.click(screen.getByRole("button", { name: /tools/i }));
    expect(screen.getByText("Wiki")).toBeInTheDocument();

    // Click the fixed backdrop (the first fixed inset-0 div in the popover)
    const backdrop = document.querySelector(".fixed.inset-0") as HTMLElement;
    await user.click(backdrop);

    // Popover content should be gone
    expect(screen.queryByText("Wiki")).not.toBeInTheDocument();
  });

  it("toggles aria-pressed based on disabledTools", async () => {
    const user = userEvent.setup();
    renderBar({ disabledTools: ["web_search"] });

    await user.click(screen.getByRole("button", { name: /tools/i }));

    const disabledToggle = screen.getByRole("switch", { name: /web_search/i });
    expect(disabledToggle).toHaveAttribute("aria-pressed", "false");

    const enabledToggle = screen.getByRole("switch", { name: /wiki_search/i });
    expect(enabledToggle).toHaveAttribute("aria-pressed", "true");
  });
});
