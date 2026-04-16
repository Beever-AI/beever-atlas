import { describe, it, expect } from "vitest";
import { sanitizeSvg } from "../sanitizeSvg";

describe("sanitizeSvg", () => {
  it("preserves <style> tags so mermaid node labels remain visible", () => {
    const input = `<svg xmlns="http://www.w3.org/2000/svg"><style>.nodeLabel{fill:#fff}</style><text class="nodeLabel">hi</text></svg>`;
    const out = sanitizeSvg(input);
    expect(out).toContain("<style");
    expect(out).toContain(".nodeLabel{fill:#fff}");
  });

  it("preserves <foreignObject> used by htmlLabels", () => {
    // The <foreignObject> tag itself must survive — mermaid emits it when
    // htmlLabels=true and relies on the browser to render namespaced XHTML
    // children. Testing the tag survival is sufficient; children rendering
    // is mermaid's responsibility.
    const input = `<svg xmlns="http://www.w3.org/2000/svg"><foreignObject width="100" height="40"></foreignObject></svg>`;
    const out = sanitizeSvg(input);
    expect(out).toContain("<foreignObject");
    expect(out).toContain('width="100"');
  });

  // GHSA-39q2-94rc-95cp regression guard: ADD_TAGS must not short-circuit FORBID_TAGS.
  // <script> must be stripped even when adjacent to allow-listed tags like <style>
  // and <foreignObject>.
  it("strips <script> even adjacent to ADD_TAGS-allowed tags", () => {
    const input = `<svg xmlns="http://www.w3.org/2000/svg">
      <style>.x{}</style>
      <script>alert(1)</script>
      <foreignObject><script>alert(2)</script></foreignObject>
    </svg>`;
    const out = sanitizeSvg(input);
    expect(out).not.toContain("<script");
    expect(out).not.toContain("alert(1)");
    expect(out).not.toContain("alert(2)");
  });

  it("strips inline event-handler attributes", () => {
    const input = `<svg xmlns="http://www.w3.org/2000/svg"><g onclick="alert(1)" onload="alert(2)" onerror="alert(3)"><text onmouseover="alert(4)">x</text></g></svg>`;
    const out = sanitizeSvg(input);
    expect(out).not.toMatch(/onclick=/i);
    expect(out).not.toMatch(/onload=/i);
    expect(out).not.toMatch(/onerror=/i);
    expect(out).not.toMatch(/onmouseover=/i);
  });
});
