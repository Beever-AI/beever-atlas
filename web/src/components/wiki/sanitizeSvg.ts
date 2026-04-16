import DOMPurify from "dompurify";

// mermaid (v11, securityLevel: "strict") already runs DOMPurify internally.
// Our outer pass must preserve:
//   - <foreignObject> + HTML children (if htmlLabels=true)
//   - <style> tags (mermaid injects CSS that colors node labels — stripping
//     these leaves node text invisible, not absent)
//   - class / style attributes (node text uses class="nodeLabel" + fill)
// Security invariant: <script> and inline event-handler attributes must be
// stripped even when adjacent to ADD_TAGS-allowed tags (GHSA-39q2-94rc-95cp).
export function sanitizeSvg(svg: string): string {
  return DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true, html: true },
    ADD_TAGS: ["style", "foreignObject"],
    ADD_ATTR: ["class", "style", "transform", "xmlns"],
    FORBID_TAGS: ["script"],
    FORBID_ATTR: [
      "onerror", "onload", "onclick", "onmouseover", "onmousedown",
      "onmouseup", "onfocus", "onblur", "onchange", "onsubmit",
    ],
  });
}
