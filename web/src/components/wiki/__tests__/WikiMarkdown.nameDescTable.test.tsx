/**
 * WikiMarkdown — name-description bullet → styled table promotion.
 *
 * LLM-generated wiki bodies frequently emit lists of the shape
 * ``- **Name** — description``. The renderer detects when ALL items
 * in a ``<ul>`` match this pattern AND there are ≥3 items, and
 * promotes the list to a 2-column ``<table>`` so the structured
 * data reads as such instead of as a sea of bullets.
 *
 * These tests pin the behaviour at the boundaries:
 *   - ≥3 matching items → table render
 *   - <3 matching items → bullet-list render (no over-eager promotion)
 *   - Mixed list (one non-matching item) → bullet-list render
 *   - Multiple separator forms (em-dash, en-dash, hyphen, colon) accepted
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { WikiMarkdown } from "../WikiMarkdown";

afterEach(() => {
  cleanup();
});

function renderInRoute(ui: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={["/channels/c1/wiki"]}>
      <Routes>
        <Route path="/channels/:id/*" element={ui} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("WikiMarkdown — name-description table promotion", () => {
  it("promotes ≥3 ``**Name** — desc`` bullets to a 2-column table", () => {
    const content = [
      "- **Repository** — Beever Atlas Documentation Repo",
      "- **Public Visibility** — Both repos are public for community access",
      "- **Mattermost Permissions** — Full administrative access required",
    ].join("\n");
    renderInRoute(<WikiMarkdown content={content} />);

    const table = document.querySelector("table");
    expect(table).not.toBeNull();
    // No <ul> falling through.
    expect(document.querySelector("ul")).toBeNull();

    // Header columns.
    expect(screen.getByText(/^name$/i)).toBeInTheDocument();
    expect(screen.getByText(/^description$/i)).toBeInTheDocument();

    // Each row keeps its name + description in two cells.
    const rows = within(table as HTMLTableElement).getAllByRole("row");
    // 1 header + 3 data rows
    expect(rows.length).toBe(4);
    expect(rows[1].textContent).toContain("Repository");
    expect(rows[1].textContent).toContain(
      "Beever Atlas Documentation Repo",
    );
    expect(rows[3].textContent).toContain("Mattermost Permissions");
  });

  it("renders <3 matching items as a plain bullet list (no over-eager promotion)", () => {
    const content = [
      "- **Repo** — alpha",
      "- **Permissions** — beta",
    ].join("\n");
    renderInRoute(<WikiMarkdown content={content} />);

    expect(document.querySelector("ul")).not.toBeNull();
    expect(document.querySelector("table")).toBeNull();
  });

  it("falls back to bullets when even one item is not a name-desc row", () => {
    const content = [
      "- **Repo** — alpha",
      "- **Permissions** — beta",
      "- A free-form note that isn't a name-desc pair",
    ].join("\n");
    renderInRoute(<WikiMarkdown content={content} />);

    expect(document.querySelector("ul")).not.toBeNull();
    expect(document.querySelector("table")).toBeNull();
  });

  it("accepts multiple separator forms (em-dash, hyphen, colon)", () => {
    const content = [
      "- **Em-dash row** — first",
      "- **Hyphen row** - second",
      "- **Colon row**: third",
    ].join("\n");
    renderInRoute(<WikiMarkdown content={content} />);

    const table = document.querySelector("table");
    expect(table).not.toBeNull();
    const rows = within(table as HTMLTableElement).getAllByRole("row");
    expect(rows.length).toBe(4);
    expect(rows[1].textContent).toContain("first");
    expect(rows[2].textContent).toContain("second");
    expect(rows[3].textContent).toContain("third");
  });

  it("does NOT promote a list of bare bold items with no separator", () => {
    // Bold-only items — no separator after — should keep bullet shape;
    // they're a glossary-style list, not name-description data.
    const content = [
      "- **Alpha**",
      "- **Beta**",
      "- **Gamma**",
    ].join("\n");
    renderInRoute(<WikiMarkdown content={content} />);

    expect(document.querySelector("ul")).not.toBeNull();
    expect(document.querySelector("table")).toBeNull();
  });
});
