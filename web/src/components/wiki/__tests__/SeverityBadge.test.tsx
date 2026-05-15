/**
 * Tests for the SeverityBadge component.
 *
 * Coverage:
 *  - Renders for each severity bucket (critical / high / medium / low)
 *  - aria-label maps to the correct human description
 *  - Unknown / missing severity falls back to medium (no blank render)
 *  - showDot=false hides the colored dot
 *  - data-severity attribute exposes the resolved bucket for tests
 */

import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { SeverityBadge } from "../SeverityBadge";

afterEach(() => cleanup());

describe("SeverityBadge", () => {
  it("renders the critical badge with the matching aria-label", () => {
    render(<SeverityBadge severity="critical" />);
    const badge = screen.getByTestId("severity-badge");
    expect(badge).toHaveAttribute("aria-label", "Critical importance");
    expect(badge.getAttribute("data-severity")).toBe("critical");
  });

  it("renders the high badge with the matching aria-label", () => {
    render(<SeverityBadge severity="high" />);
    const badge = screen.getByTestId("severity-badge");
    expect(badge).toHaveAttribute("aria-label", "High importance");
    expect(badge.getAttribute("data-severity")).toBe("high");
  });

  it("renders the medium badge with the matching aria-label", () => {
    render(<SeverityBadge severity="medium" />);
    const badge = screen.getByTestId("severity-badge");
    expect(badge).toHaveAttribute("aria-label", "Medium importance");
    expect(badge.getAttribute("data-severity")).toBe("medium");
  });

  it("renders the low badge with the matching aria-label", () => {
    render(<SeverityBadge severity="low" />);
    const badge = screen.getByTestId("severity-badge");
    expect(badge).toHaveAttribute("aria-label", "Low importance");
    expect(badge.getAttribute("data-severity")).toBe("low");
  });

  it("falls back to medium for unknown severity values", () => {
    render(<SeverityBadge severity="banana" />);
    const badge = screen.getByTestId("severity-badge");
    // Bucket falls back to medium config (label) but data-severity
    // surfaces the raw input so tests / debuggers can see what came in.
    expect(badge).toHaveAttribute("aria-label", "Medium importance");
    expect(badge.getAttribute("data-severity")).toBe("banana");
  });

  it("falls back to medium when severity is null/undefined", () => {
    render(<SeverityBadge severity={null} />);
    const badge = screen.getByTestId("severity-badge");
    expect(badge).toHaveAttribute("aria-label", "Medium importance");
  });

  it("renders the colored dot by default", () => {
    render(<SeverityBadge severity="critical" />);
    expect(screen.getByTestId("severity-badge-dot")).toBeInTheDocument();
  });

  it("hides the colored dot when showDot=false", () => {
    render(<SeverityBadge severity="critical" showDot={false} />);
    expect(
      screen.queryByTestId("severity-badge-dot"),
    ).not.toBeInTheDocument();
  });

  it("respects a custom aria-label override", () => {
    render(
      <SeverityBadge severity="high" ariaLabel="Customised label" />,
    );
    expect(screen.getByTestId("severity-badge")).toHaveAttribute(
      "aria-label",
      "Customised label",
    );
  });

  it("renders an icon (lucide svg) inside the badge", () => {
    const { container } = render(<SeverityBadge severity="critical" />);
    // Lucide icons render as <svg> children. We don't pin a specific
    // svg name (lucide internals can shift), just assert one exists.
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
  });
});
