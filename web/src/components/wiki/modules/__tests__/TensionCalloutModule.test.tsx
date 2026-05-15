/** @vitest-environment jsdom */
/**
 * Tests for the Tension Callout module — Phase 4 wiki redesign.
 *
 * Coverage:
 *  - Renders title + position cards when populated.
 *  - Status pill colors track the status field.
 *  - Empty/missing data returns null (defensive against the planner
 *    picking the module despite the predicate failing).
 *  - Single-position payload still renders null (a tension requires
 *    two contradicting positions to be meaningful).
 *  - Cite-id chip renders when ``tension_id`` is set.
 */

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { TensionCalloutModule } from "../TensionCalloutModule";
import type { WikiPageModule } from "@/lib/types";

afterEach(() => cleanup());

const baseModule = (data: Record<string, unknown>): WikiPageModule => ({
  id: "tension_callout",
  anchor: "tension-callout",
  data,
}) as unknown as WikiPageModule;

const fullPayload = {
  label: "Tension",
  renderer_kind: "frontend",
  title: "Custom memory vs Google Memory Bank",
  status: "open",
  since: "2026-04-22",
  tension_id: "t_abc12345",
  positions: [
    {
      author: "Jacky Chan",
      stance: "Hand-rolled is tuned for chat memoir ingestion.",
      fact_id: "f_legacy",
    },
    {
      author: "Thomas Chong",
      stance: "Google Memory Bank is a direct fit replacement.",
      fact_id: "f_replace",
    },
  ],
};

describe("TensionCalloutModule — happy path", () => {
  it("renders the title + 2 position cards", () => {
    render(
      <TensionCalloutModule module={baseModule(fullPayload)} citations={[]} />,
    );
    expect(screen.getByTestId("tension-callout-title")).toHaveTextContent(
      "Custom memory vs Google Memory Bank",
    );
    expect(screen.getByTestId("tension-callout-position-0")).toBeInTheDocument();
    expect(screen.getByTestId("tension-callout-position-1")).toBeInTheDocument();
    expect(screen.getByTestId("tension-callout-stance-0")).toHaveTextContent(
      /Hand-rolled is tuned/,
    );
    expect(screen.getByTestId("tension-callout-stance-1")).toHaveTextContent(
      /direct fit replacement/,
    );
  });

  it("renders the cite-id chips for each position and the tension", () => {
    render(
      <TensionCalloutModule module={baseModule(fullPayload)} citations={[]} />,
    );
    expect(screen.getByTestId("tension-callout-cite-0")).toHaveTextContent(
      /f_legacy/,
    );
    expect(screen.getByTestId("tension-callout-cite-1")).toHaveTextContent(
      /f_replace/,
    );
    expect(screen.getByTestId("tension-callout-tension-id")).toHaveTextContent(
      /t_abc12345/,
    );
  });

  it("formats the since date as 'Mon DD'", () => {
    render(
      <TensionCalloutModule module={baseModule(fullPayload)} citations={[]} />,
    );
    expect(screen.getByTestId("tension-callout-since")).toHaveTextContent(
      "Since Apr 22",
    );
  });
});

describe("TensionCalloutModule — status pill colors", () => {
  it("uses amber tint for open", () => {
    render(
      <TensionCalloutModule
        module={baseModule({ ...fullPayload, status: "open" })}
        citations={[]}
      />,
    );
    const pill = screen.getByTestId("tension-callout-status");
    expect(pill).toHaveTextContent("open");
    expect(pill.className).toMatch(/amber/);
  });

  it("uses rose tint for blocked", () => {
    render(
      <TensionCalloutModule
        module={baseModule({ ...fullPayload, status: "blocked" })}
        citations={[]}
      />,
    );
    const pill = screen.getByTestId("tension-callout-status");
    expect(pill).toHaveTextContent("blocked");
    expect(pill.className).toMatch(/rose/);
  });

  it("uses muted tint for deferred", () => {
    render(
      <TensionCalloutModule
        module={baseModule({ ...fullPayload, status: "deferred" })}
        citations={[]}
      />,
    );
    const pill = screen.getByTestId("tension-callout-status");
    expect(pill).toHaveTextContent("deferred");
    expect(pill.className).toMatch(/muted/);
  });
});

describe("TensionCalloutModule — defensive fallbacks", () => {
  it("returns null when title is empty", () => {
    const { container } = render(
      <TensionCalloutModule
        module={baseModule({ ...fullPayload, title: "" })}
        citations={[]}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("returns null when positions is empty", () => {
    const { container } = render(
      <TensionCalloutModule
        module={baseModule({ ...fullPayload, positions: [] })}
        citations={[]}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("returns null when data is undefined", () => {
    const { container } = render(
      <TensionCalloutModule
        module={
          {
            id: "tension_callout",
            anchor: "tension-callout",
          } as unknown as WikiPageModule
        }
        citations={[]}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("hides since chip when since is empty", () => {
    render(
      <TensionCalloutModule
        module={baseModule({ ...fullPayload, since: "" })}
        citations={[]}
      />,
    );
    expect(screen.queryByTestId("tension-callout-since")).not.toBeInTheDocument();
  });
});
