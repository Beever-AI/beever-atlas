/** @vitest-environment jsdom */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { DecisionBannerModule } from "../DecisionBannerModule";
import type { WikiPageModule } from "@/lib/types";

const baseModule = (data: Record<string, unknown>): WikiPageModule => ({
  id: "decision_banner",
  anchor: "decision-banner",
  data,
}) as unknown as WikiPageModule;

describe("DecisionBannerModule", () => {
  it("renders the headline + body when both are present", () => {
    render(
      <DecisionBannerModule
        module={baseModule({
          decision: "Adopt a Copyright-assignment CLA.",
          body: "It provides relicensing flexibility.",
          decided_by: { name: "Jacky Chan", fact_id: "f_1" },
          decided_at: "2026-04-29",
          rationale: null,
          alternatives_rejected: [],
          consequences_open: [],
          fact_id: "f_1",
        })}
        citations={[]}
      />,
    );
    expect(screen.getByTestId("decision-banner-decision")).toHaveTextContent(
      "Adopt a Copyright-assignment CLA.",
    );
    expect(screen.getByTestId("decision-banner-body")).toHaveTextContent(
      "It provides relicensing flexibility.",
    );
    expect(screen.getByTestId("decision-banner-author")).toHaveTextContent(
      "Jacky Chan",
    );
  });

  it("formats the date as 'Mon DD, YYYY'", () => {
    render(
      <DecisionBannerModule
        module={baseModule({
          decision: "Adopt CLA.",
          decided_at: "2026-04-29",
          decided_by: { name: "Jacky Chan", fact_id: "f_1" },
        })}
        citations={[]}
      />,
    );
    expect(screen.getByTestId("decision-banner-date")).toHaveTextContent(
      "Apr 29, 2026",
    );
  });

  it("hides the body row when body is empty", () => {
    render(
      <DecisionBannerModule
        module={baseModule({
          decision: "Adopt CLA.",
          body: "",
          decided_at: "2026-04-29",
          decided_by: { name: "Jacky Chan", fact_id: "f_1" },
        })}
        citations={[]}
      />,
    );
    expect(screen.queryByTestId("decision-banner-body")).toBeNull();
  });

  it("hides the rationale row when rationale is null (Phase 3 placeholder)", () => {
    render(
      <DecisionBannerModule
        module={baseModule({
          decision: "Adopt CLA.",
          rationale: null,
          decided_by: { name: "Jacky Chan", fact_id: "f_1" },
        })}
        citations={[]}
      />,
    );
    expect(screen.queryByTestId("decision-banner-rationale")).toBeNull();
  });

  it("renders the rationale row when rationale is provided (Phase 3 future)", () => {
    render(
      <DecisionBannerModule
        module={baseModule({
          decision: "Adopt CLA.",
          rationale: "Provides relicensing flexibility for future commercial forks.",
          decided_by: { name: "Jacky Chan", fact_id: "f_1" },
        })}
        citations={[]}
      />,
    );
    const rationale = screen.getByTestId("decision-banner-rationale");
    expect(rationale).toHaveTextContent("Because:");
    expect(rationale).toHaveTextContent(
      "Provides relicensing flexibility for future commercial forks.",
    );
  });

  it("hides alternatives row when the list is empty", () => {
    render(
      <DecisionBannerModule
        module={baseModule({
          decision: "Adopt CLA.",
          alternatives_rejected: [],
        })}
        citations={[]}
      />,
    );
    expect(screen.queryByTestId("decision-banner-alternatives")).toBeNull();
  });

  it("renders alternatives when present", () => {
    render(
      <DecisionBannerModule
        module={baseModule({
          decision: "Adopt CLA.",
          alternatives_rejected: ["DCO", "License-grant CLA"],
        })}
        citations={[]}
      />,
    );
    const alts = screen.getByTestId("decision-banner-alternatives");
    expect(alts).toHaveTextContent("DCO");
    expect(alts).toHaveTextContent("License-grant CLA");
    expect(alts).toHaveTextContent("Alternatives rejected");
  });

  it("renders consequences when present", () => {
    render(
      <DecisionBannerModule
        module={baseModule({
          decision: "Adopt CLA.",
          consequences_open: ["Will external contributors hesitate?"],
        })}
        citations={[]}
      />,
    );
    const csq = screen.getByTestId("decision-banner-consequences");
    expect(csq).toHaveTextContent("Will external contributors hesitate?");
    expect(csq).toHaveTextContent("Open consequences");
  });

  it("renders the cite-id chip and source link in the footer", () => {
    render(
      <DecisionBannerModule
        module={baseModule({
          decision: "Adopt CLA.",
          fact_id: "f_cla_decision",
          source_url: "https://team.votee.com/some/message",
        })}
        citations={[]}
      />,
    );
    const factIdChip = screen.getByTestId("decision-banner-fact-id");
    expect(factIdChip).toHaveTextContent("Cite as: f_cla_decision");
    const sourceLink = screen.getByTestId("decision-banner-source-link");
    expect(sourceLink).toHaveAttribute(
      "href",
      "https://team.votee.com/some/message",
    );
    expect(sourceLink).toHaveAttribute("target", "_blank");
  });

  it("returns null when decision is empty (defensive against bad picks)", () => {
    const { container } = render(
      <DecisionBannerModule
        module={baseModule({
          decision: "",
          decided_by: { name: "", fact_id: "" },
        })}
        citations={[]}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("falls back to raw date string when format is unrecognised", () => {
    render(
      <DecisionBannerModule
        module={baseModule({
          decision: "Adopt CLA.",
          decided_at: "yesterday",
        })}
        citations={[]}
      />,
    );
    // Malformed date should not break rendering; chip should still
    // appear with the raw value rather than crash or hide silently.
    expect(screen.getByTestId("decision-banner-date")).toHaveTextContent(
      "yesterday",
    );
  });
});
