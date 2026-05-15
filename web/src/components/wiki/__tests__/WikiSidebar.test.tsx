/**
 * Tests for WikiSidebar — icon dispatch + a11y semantics.
 *
 * Coverage:
 *  - Folder rows render with the folder testid + data-folder attr
 *  - Leaf topic rows render the FileText icon (sidebar-icon-leaf testid)
 *  - Fixed pages render their icon (sidebar-icon-fixed testid)
 *  - aria-current="page" lands on the active row
 *  - aria-expanded reflects folder expanded state
 *
 * Privacy: all fixtures use synthetic placeholder titles.
 */

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { WikiSidebar } from "../WikiSidebar";
import type { WikiPageNode } from "@/lib/types";

afterEach(() => cleanup());

function makeNode(overrides: Partial<WikiPageNode>): WikiPageNode {
  return {
    id: overrides.id ?? "node-1",
    title: overrides.title ?? "Node",
    slug: overrides.slug ?? "node",
    section_number: overrides.section_number ?? "",
    page_type: overrides.page_type ?? "topic",
    memory_count: overrides.memory_count ?? 0,
    children: overrides.children ?? [],
    is_synthetic: overrides.is_synthetic,
    summary: overrides.summary,
  };
}

describe("WikiSidebar — icon dispatch", () => {
  it("renders the FileText leaf icon for childless topic pages", () => {
    const pages: WikiPageNode[] = [
      makeNode({
        id: "topic-leaf-1",
        title: "Leaf Topic",
        slug: "leaf-topic",
        section_number: "2.1",
        page_type: "topic",
      }),
    ];
    render(
      <WikiSidebar
        pages={pages}
        activePageId="topic-leaf-1"
        onNavigate={() => undefined}
      />,
    );
    expect(screen.getByTestId("sidebar-icon-leaf")).toBeInTheDocument();
  });

  it("renders a fixed-page icon (sidebar-icon-fixed) for fixed pages", () => {
    const pages: WikiPageNode[] = [
      makeNode({
        id: "overview",
        title: "Overview",
        slug: "overview",
        page_type: "fixed",
      }),
    ];
    render(
      <WikiSidebar
        pages={pages}
        activePageId="overview"
        onNavigate={() => undefined}
      />,
    );
    expect(screen.getByTestId("sidebar-icon-fixed")).toBeInTheDocument();
    // No leaf icon on fixed pages.
    expect(screen.queryByTestId("sidebar-icon-leaf")).not.toBeInTheDocument();
  });

  it("renders folder rows with the folder testid + data-folder attr", () => {
    const pages: WikiPageNode[] = [
      makeNode({
        id: "folder-1",
        title: "Folder One",
        slug: "folder-one",
        page_type: "folder",
        children: [
          makeNode({
            id: "topic-child-1",
            title: "Child Topic",
            slug: "child-topic",
            page_type: "topic",
          }),
        ],
      }),
    ];
    render(
      <WikiSidebar
        pages={pages}
        activePageId="folder-1"
        onNavigate={() => undefined}
      />,
    );
    const folderRow = screen.getByTestId("sidebar-folder-folder-1");
    expect(folderRow).toBeInTheDocument();
    expect(folderRow.getAttribute("data-folder")).toBe("true");
  });
});

describe("WikiSidebar — a11y", () => {
  it("marks the active leaf row with aria-current=page", () => {
    const pages: WikiPageNode[] = [
      makeNode({
        id: "topic-active",
        title: "Active Topic",
        slug: "active-topic",
        section_number: "2.1",
        page_type: "topic",
      }),
      makeNode({
        id: "topic-other",
        title: "Other Topic",
        slug: "other-topic",
        section_number: "2.2",
        page_type: "topic",
      }),
    ];
    render(
      <WikiSidebar
        pages={pages}
        activePageId="topic-active"
        onNavigate={() => undefined}
      />,
    );
    const activeBtn = screen.getByTestId("sidebar-item-topic-active");
    expect(activeBtn).toHaveAttribute("aria-current", "page");
    const otherBtn = screen.getByTestId("sidebar-item-topic-other");
    expect(otherBtn).not.toHaveAttribute("aria-current");
  });

  it("toggles folder aria-expanded + data-expanded on click", () => {
    const pages: WikiPageNode[] = [
      makeNode({
        id: "folder-toggle",
        title: "Toggle Folder",
        slug: "toggle-folder",
        page_type: "folder",
        children: [
          makeNode({
            id: "child-1",
            title: "Child One",
            slug: "child-one",
            page_type: "topic",
          }),
        ],
      }),
    ];
    render(
      <WikiSidebar
        pages={pages}
        activePageId="other-page"
        onNavigate={() => undefined}
      />,
    );
    const folderRow = screen.getByTestId("sidebar-folder-folder-toggle");
    // Auto-expand at depth 1 → expanded by default.
    expect(folderRow.getAttribute("data-expanded")).toBe("true");
    // Click the chevron to collapse.
    const collapseBtn = screen.getByLabelText("Collapse folder");
    fireEvent.click(collapseBtn);
    expect(folderRow.getAttribute("data-expanded")).toBe("false");
  });
});
