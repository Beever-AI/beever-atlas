import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TensionsSection } from "../TensionsSection";
import type { WikiTension } from "../TensionsSection";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const tension1: WikiTension = {
  fact_id: "fact-aabbccdd-1234-5678-abcd-000000000001",
  contradicts_fact_id: "fact-aabbccdd-1234-5678-abcd-000000000002",
  summary: "Fact A says the deadline is Q1; Fact B says it is Q2.",
  detected_at: new Date().toISOString(),
};

const tension2: WikiTension = {
  fact_id: "fact-aabbccdd-1234-5678-abcd-000000000003",
  contradicts_fact_id: "fact-aabbccdd-1234-5678-abcd-000000000004",
  // No summary — exercises the fallback rendering path
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("TensionsSection", () => {
  it("renders nothing when tensions is undefined", () => {
    const { container } = render(
      <TensionsSection tensions={undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when tensions is an empty array", () => {
    const { container } = render(<TensionsSection tensions={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a section header when tensions are present", () => {
    render(<TensionsSection tensions={[tension1]} />);
    expect(screen.getByRole("heading", { name: /tensions/i })).toBeInTheDocument();
  });

  it("renders the count of unresolved tensions", () => {
    render(<TensionsSection tensions={[tension1, tension2]} />);
    expect(screen.getByText(/2 unresolved/i)).toBeInTheDocument();
  });

  it("renders tension summary text when provided", () => {
    render(<TensionsSection tensions={[tension1]} />);
    expect(screen.getByText(/deadline is Q1/i)).toBeInTheDocument();
  });

  it("renders fallback fact-id snippet when summary is absent", () => {
    const { container } = render(<TensionsSection tensions={[tension2]} />);
    // The component renders t.fact_id.slice(0, 8) inside a <code> element.
    // fact_id = "fact-aabbccdd-...", slice(0,8) = "fact-aab"
    // Text is split across multiple inline nodes so use the rendered container.
    const codes = container.querySelectorAll("code");
    const found = Array.from(codes).some((el) =>
      (el.textContent ?? "").toLowerCase().startsWith("fact-aab"),
    );
    expect(found).toBe(true);
  });

  it("renders Mark resolved button when onResolve is provided", () => {
    render(
      <TensionsSection tensions={[tension1]} onResolve={vi.fn()} />,
    );
    expect(
      screen.getByRole("button", { name: /mark resolved/i }),
    ).toBeInTheDocument();
  });

  it("does not render Mark resolved button when onResolve is absent", () => {
    render(<TensionsSection tensions={[tension1]} />);
    expect(
      screen.queryByRole("button", { name: /mark resolved/i }),
    ).not.toBeInTheDocument();
  });

  it("calls onResolve with the correct fact_id and contradicts_fact_id on click", async () => {
    const onResolve = vi.fn();
    render(<TensionsSection tensions={[tension1]} onResolve={onResolve} />);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /mark resolved/i }));

    expect(onResolve).toHaveBeenCalledOnce();
    expect(onResolve).toHaveBeenCalledWith(
      tension1.fact_id,
      tension1.contradicts_fact_id,
    );
  });

  it("renders multiple tensions as separate list items", () => {
    render(
      <TensionsSection tensions={[tension1, tension2]} onResolve={vi.fn()} />,
    );
    const buttons = screen.getAllByRole("button", { name: /mark resolved/i });
    expect(buttons).toHaveLength(2);
  });
});
