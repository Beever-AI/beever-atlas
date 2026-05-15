/**
 * Tests for the ProvenanceDrawerModule.
 *
 * Coverage:
 *  - Default-collapsed state (toggle hides messages on first render)
 *  - Expand toggles open the message list
 *  - First 10 messages render eagerly when expanded; "Show N more"
 *    expander reveals the remainder
 *  - Author / platform / channel pills + fact_id chips render
 *  - "Open ↗" external link surfaces when URL is set
 *  - Empty messages → null render
 */

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { ProvenanceDrawerModule } from "../ProvenanceDrawerModule";
import type { WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

interface MessageFixture {
  ts?: string;
  author?: string;
  platform?: string;
  channel?: string;
  url?: string;
  snippet?: string;
  contributed_to_facts?: string[];
}

function makeModule(messages: MessageFixture[] = [], totalCount?: number): WikiPageModule {
  return {
    id: "provenance_drawer",
    anchor: "provenance",
    data: {
      label: "Source messages",
      renderer_kind: "frontend",
      messages,
      total_count: totalCount ?? messages.length,
    },
  };
}

const noop = () => undefined;

describe("ProvenanceDrawerModule", () => {
  it("renders nothing when messages is empty", () => {
    const { container } = render(
      <ProvenanceDrawerModule
        module={makeModule([])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders the toggle button with the total count", () => {
    render(
      <ProvenanceDrawerModule
        module={makeModule(
          [
            {
              ts: "2026-04-22T10:32:00Z",
              author: "Jacky",
              platform: "mattermost",
              snippet: "Forked beever-atlas as legacy-memory.",
              contributed_to_facts: ["f1"],
            },
          ],
          12,
        )}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.getByTestId("provenance-drawer-toggle")).toBeInTheDocument();
    expect(screen.getByText(/Source messages \(12\)/)).toBeInTheDocument();
  });

  it("starts collapsed — message list is not in the DOM until clicked", () => {
    render(
      <ProvenanceDrawerModule
        module={makeModule([
          {
            ts: "2026-04-22T10:32:00Z",
            author: "Jacky",
            snippet: "First message.",
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    expect(screen.queryByTestId("provenance-drawer-list")).not.toBeInTheDocument();
  });

  it("expands the message list when the toggle is clicked", () => {
    render(
      <ProvenanceDrawerModule
        module={makeModule([
          {
            ts: "2026-04-22T10:32:00Z",
            author: "Jacky",
            platform: "mattermost",
            snippet: "First message.",
            contributed_to_facts: ["f_alpha"],
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    fireEvent.click(screen.getByTestId("provenance-drawer-toggle"));
    expect(screen.getByTestId("provenance-drawer-list")).toBeInTheDocument();
    expect(screen.getByTestId("provenance-message")).toBeInTheDocument();
    expect(screen.getByTestId("provenance-author-chip")).toHaveTextContent("@Jacky");
    expect(screen.getByTestId("provenance-platform-pill")).toHaveTextContent("mattermost");
    expect(screen.getByText(/First message/)).toBeInTheDocument();
    expect(screen.getByTestId("provenance-fact-chip")).toHaveTextContent("f_alpha");
  });

  it("renders an Open ↗ link when url is present", () => {
    render(
      <ProvenanceDrawerModule
        module={makeModule([
          {
            ts: "2026-04-22T10:32:00Z",
            author: "Jacky",
            url: "https://team.votee.com/post/abc",
            snippet: "x",
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    fireEvent.click(screen.getByTestId("provenance-drawer-toggle"));
    const link = screen.getByText("Open ↗");
    expect(link).toBeInTheDocument();
    expect(link.closest("a")).toHaveAttribute(
      "href",
      "https://team.votee.com/post/abc",
    );
  });

  it("shows only the first 10 messages with a 'Show N more' expander", () => {
    const messages = Array.from({ length: 15 }).map((_, i) => ({
      ts: `2026-04-${String(i + 1).padStart(2, "0")}T09:00:00Z`,
      author: `User${i}`,
      snippet: `Message ${i}`,
    }));
    render(
      <ProvenanceDrawerModule
        module={makeModule(messages)}
        citations={[]}
        onNavigate={noop}
      />,
    );
    fireEvent.click(screen.getByTestId("provenance-drawer-toggle"));
    expect(screen.getAllByTestId("provenance-message").length).toBe(10);
    const expander = screen.getByTestId("provenance-show-more");
    expect(expander).toHaveTextContent("Show 5 more");
    fireEvent.click(expander);
    expect(screen.getAllByTestId("provenance-message").length).toBe(15);
    expect(screen.queryByTestId("provenance-show-more")).not.toBeInTheDocument();
  });

  it("renders the channel pill when channel is set", () => {
    render(
      <ProvenanceDrawerModule
        module={makeModule([
          {
            ts: "2026-04-22T10:32:00Z",
            author: "Jacky",
            channel: "tech-beever-atlas",
            snippet: "x",
          },
        ])}
        citations={[]}
        onNavigate={noop}
      />,
    );
    fireEvent.click(screen.getByTestId("provenance-drawer-toggle"));
    expect(screen.getByTestId("provenance-channel-pill")).toHaveTextContent(
      "#tech-beever-atlas",
    );
  });
});
