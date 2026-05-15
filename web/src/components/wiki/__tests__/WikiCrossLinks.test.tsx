/**
 * Frontend test for the wiki-llm-native-redesign cross-links renderer
 * (task §4.14). Verifies that:
 *   - Resolved [[Title]] references render as clickable anchors to
 *     /channels/:id/wiki/:slug (path-based routing, see ``buildWikiPath``)
 *   - Unresolved (broken) references render as red-dashed buttons
 *     that open a "No page yet" modal
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
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

describe("WikiMarkdown — [[wikilink]] renderer", () => {
  it("renders a resolved wikilink as a clickable anchor", () => {
    renderInRoute(
      <WikiMarkdown
        content="See [[Authentication]] for context."
        crossLinks={{ Authentication: "topic-auth" }}
      />,
    );
    const anchor = screen.getByTestId("wiki-link");
    expect(anchor).toHaveAttribute(
      "href",
      "/channels/c1/wiki/topic-auth",
    );
    expect(anchor).toHaveTextContent("Authentication");
    expect(anchor).toHaveAttribute("data-slug", "topic-auth");
  });

  it("renders an unresolved [[Title]] as a broken-link button", () => {
    renderInRoute(
      <WikiMarkdown
        content="Need [[Logging Strategy]] eventually."
        crossLinks={{}}
        crossLinksBroken={["Logging Strategy"]}
      />,
    );
    const broken = screen.getByTestId("wiki-broken-link");
    expect(broken).toHaveTextContent("Logging Strategy");
    expect(broken).toHaveAttribute("data-title", "Logging Strategy");
    // The Tailwind classes for broken styling are present.
    expect(broken.className).toMatch(/text-red-/);
    expect(broken.className).toMatch(/decoration-dashed/);
  });

  it("renders [[Title]] as broken when crossLinks/crossLinksBroken are absent", () => {
    // Defensive: if the page object hasn't been migrated yet, every
    // wikilink renders as broken instead of crashing the renderer.
    renderInRoute(
      <WikiMarkdown content="Mentioning [[Authentication]] inline." />,
    );
    expect(screen.getByTestId("wiki-broken-link")).toHaveTextContent(
      "Authentication",
    );
  });

  it("clicking a broken link opens the placeholder create-page modal", () => {
    renderInRoute(
      <WikiMarkdown
        content="Need [[Logging Strategy]] eventually."
        crossLinksBroken={["Logging Strategy"]}
      />,
    );
    fireEvent.click(screen.getByTestId("wiki-broken-link"));
    const dialog = screen.getByRole("dialog", { name: /create wiki page/i });
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveTextContent(/Logging Strategy/);
    // Close button dismisses the modal.
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(
      screen.queryByRole("dialog", { name: /create wiki page/i }),
    ).not.toBeInTheDocument();
  });

  it("renders multiple wikilinks in one paragraph independently", () => {
    renderInRoute(
      <WikiMarkdown
        content="Both [[Authentication]] and [[Sessions]] matter; [[Unknown]] does not yet."
        crossLinks={{ Authentication: "topic-auth", Sessions: "topic-sessions" }}
        crossLinksBroken={["Unknown"]}
      />,
    );
    // Two resolved anchors, one broken button.
    const anchors = screen.getAllByTestId("wiki-link");
    expect(anchors).toHaveLength(2);
    const slugs = anchors.map((a) => a.getAttribute("data-slug")).sort();
    expect(slugs).toEqual(["topic-auth", "topic-sessions"]);
    expect(screen.getByTestId("wiki-broken-link")).toHaveTextContent(
      "Unknown",
    );
  });

  it("does NOT mistake a numeric citation [1] for a wikilink", () => {
    // Sanity: the wikilink regex requires double brackets, so a single
    // [1] still flows through the legacy citation path. We assert the
    // broken-link path was NOT taken — would catch a regex regression.
    renderInRoute(
      <WikiMarkdown
        content="Confirmed by [1]."
        citations={[
          {
            id: "src1",
            author: "Alice",
            channel: "engineering",
            timestamp: "2026-01-15T00:00:00Z",
            text_excerpt: "Auth uses OAuth2",
            permalink: "https://example.com/msg/1",
          },
        ]}
      />,
    );
    expect(screen.queryByTestId("wiki-broken-link")).toBeNull();
  });
});
