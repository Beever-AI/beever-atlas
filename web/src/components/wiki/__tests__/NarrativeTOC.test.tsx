/**
 * Tests for the NarrativeTOC component.
 *
 * Coverage:
 *  - Hidden when sections.length < 3 (returns null entirely)
 *  - Renders sticky panel + dropdown wrappers when sections.length >= 3
 *    (Tailwind handles the lg:hidden / hidden lg:block media gating;
 *    we assert both wrappers are present in the DOM)
 *  - Anchor link click smooth-scrolls to the section
 *  - Dropdown change smooth-scrolls to the selected section
 *  - Each section heading appears in both nav surfaces
 *  - The native <select> element is used (accessibility: dropdown
 *    keyboard navigation comes for free)
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { fireEvent, render, screen, cleanup } from "@testing-library/react";
import { NarrativeTOC } from "../NarrativeTOC";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const sectionsThree = [
  { anchor: "intro", heading: "Introduction" },
  { anchor: "details", heading: "Details" },
  { anchor: "outlook", heading: "Outlook" },
];

// ---------------------------------------------------------------------------
// Visibility gating
// ---------------------------------------------------------------------------

describe("NarrativeTOC — visibility gating", () => {
  it("renders nothing when sections.length is 0", () => {
    const { container } = render(<NarrativeTOC sections={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when sections.length is 2 (under the 3-section threshold)", () => {
    const { container } = render(
      <NarrativeTOC
        sections={[
          { anchor: "a", heading: "Alpha" },
          { anchor: "b", heading: "Beta" },
        ]}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders both nav surfaces when sections.length >= 3", () => {
    render(<NarrativeTOC sections={sectionsThree} />);
    expect(screen.getByTestId("narrative-toc-sticky")).toBeInTheDocument();
    expect(screen.getByTestId("narrative-toc-dropdown")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Sticky panel
// ---------------------------------------------------------------------------

describe("NarrativeTOC — sticky panel", () => {
  it("renders one anchor link per section with `href={#anchor}`", () => {
    render(<NarrativeTOC sections={sectionsThree} />);
    const links = screen.getAllByTestId("narrative-toc-link");
    expect(links).toHaveLength(3);
    expect(links[0].getAttribute("href")).toBe("#intro");
    expect(links[1].getAttribute("href")).toBe("#details");
    expect(links[2].getAttribute("href")).toBe("#outlook");
  });

  it("displays each section heading text", () => {
    render(<NarrativeTOC sections={sectionsThree} />);
    const sticky = screen.getByTestId("narrative-toc-sticky");
    expect(sticky.textContent || "").toContain("Introduction");
    expect(sticky.textContent || "").toContain("Details");
    expect(sticky.textContent || "").toContain("Outlook");
  });

  it("anchor link click smooth-scrolls to the matching section", () => {
    // Inject DOM nodes the TOC will look up via getElementById.
    const intro = document.createElement("section");
    intro.id = "intro";
    intro.scrollIntoView = vi.fn();
    document.body.appendChild(intro);

    const details = document.createElement("section");
    details.id = "details";
    details.scrollIntoView = vi.fn();
    document.body.appendChild(details);

    const outlook = document.createElement("section");
    outlook.id = "outlook";
    outlook.scrollIntoView = vi.fn();
    document.body.appendChild(outlook);

    render(<NarrativeTOC sections={sectionsThree} />);
    const links = screen.getAllByTestId("narrative-toc-link");
    fireEvent.click(links[1]);
    expect(details.scrollIntoView).toHaveBeenCalledWith({
      behavior: "smooth",
      block: "start",
    });
    // The other anchors should not have been scrolled.
    expect(intro.scrollIntoView).not.toHaveBeenCalled();
    expect(outlook.scrollIntoView).not.toHaveBeenCalled();

    // Clean up the injected nodes so next test gets a fresh DOM.
    intro.remove();
    details.remove();
    outlook.remove();
  });
});

// ---------------------------------------------------------------------------
// Dropdown variant
// ---------------------------------------------------------------------------

describe("NarrativeTOC — dropdown variant", () => {
  it("renders a native <select> element for accessibility", () => {
    render(<NarrativeTOC sections={sectionsThree} />);
    const select = screen.getByTestId("narrative-toc-select");
    expect(select.tagName.toLowerCase()).toBe("select");
  });

  it("includes one option per section plus the placeholder", () => {
    render(<NarrativeTOC sections={sectionsThree} />);
    const select = screen.getByTestId("narrative-toc-select") as HTMLSelectElement;
    const optionTexts = Array.from(select.options).map((o) => o.textContent || "");
    // Placeholder + 3 section options
    expect(select.options).toHaveLength(4);
    expect(optionTexts).toContain("Introduction");
    expect(optionTexts).toContain("Details");
    expect(optionTexts).toContain("Outlook");
  });

  it("change event smooth-scrolls to the selected section", () => {
    const target = document.createElement("section");
    target.id = "outlook";
    target.scrollIntoView = vi.fn();
    document.body.appendChild(target);

    render(<NarrativeTOC sections={sectionsThree} />);
    const select = screen.getByTestId("narrative-toc-select") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "outlook" } });
    expect(target.scrollIntoView).toHaveBeenCalledWith({
      behavior: "smooth",
      block: "start",
    });

    target.remove();
  });

  it("placeholder option (empty value) does NOT trigger a scroll", () => {
    const intro = document.createElement("section");
    intro.id = "intro";
    intro.scrollIntoView = vi.fn();
    document.body.appendChild(intro);

    render(<NarrativeTOC sections={sectionsThree} />);
    const select = screen.getByTestId("narrative-toc-select") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "" } });
    expect(intro.scrollIntoView).not.toHaveBeenCalled();

    intro.remove();
  });
});
