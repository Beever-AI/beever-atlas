/** Tests for the LLM Overview body → structured sections parser. */
import { describe, it, expect } from "vitest";
import { parseOverviewBody } from "../overviewSections";

describe("parseOverviewBody", () => {
  it("extracts a TL;DR from the first bold-only line", () => {
    const body =
      "**JWT replaces SAML for service auth.**\n\n" +
      "The team migrated from SAML to JWT in Q1.\n";
    const out = parseOverviewBody(body);
    expect(out.tldr).toBe("JWT replaces SAML for service auth.");
    expect(out.intro).toContain("migrated from SAML");
    expect(out.intro).not.toContain("**JWT");
  });

  it("leaves intro alone when no bold-only line is present", () => {
    const body = "The channel covers auth migration.\n";
    const out = parseOverviewBody(body);
    expect(out.tldr).toBe("");
    expect(out.intro.trim()).toBe("The channel covers auth migration.");
  });

  it("extracts a mermaid block from the Concept Map section", () => {
    const body =
      "Intro.\n\n" +
      "## Concept Map\n\n" +
      "```mermaid\ngraph TD\n  A --> B\n```\n\n" +
      "## Other Section\n\nbody\n";
    const out = parseOverviewBody(body);
    expect(out.conceptMapMermaid).toContain("graph TD");
    expect(out.conceptMapMermaid).toContain("A --> B");
    expect(out.residualBody).not.toContain("```mermaid");
    expect(out.residualBody).toContain("## Other Section");
  });

  it("parses Key Contributors with grouping headers", () => {
    const body =
      "Intro.\n\n" +
      "## Key Contributors\n\n" +
      "- **Project Development & Integration**\n" +
      "- Thomas Chong: Leads technical discussions [2] [12]\n" +
      "- Jacky Chan: Drives security reviews [5]\n" +
      "- **Open-Sourcing & Promotion**\n" +
      "- Jack Ng: Focuses on outreach campaigns\n";
    const out = parseOverviewBody(body);
    expect(out.contributors.length).toBe(2);
    expect(out.contributors[0].group).toBe("Project Development & Integration");
    expect(out.contributors[0].entries.length).toBe(2);
    expect(out.contributors[0].entries[0].name).toBe("Thomas Chong");
    expect(out.contributors[0].entries[0].citations).toEqual([2, 12]);
    expect(out.contributors[1].group).toBe("Open-Sourcing & Promotion");
    expect(out.contributors[1].entries[0].name).toBe("Jack Ng");
  });

  it("parses Tools & Resources bullets into entries", () => {
    const body =
      "## Tools & Resources\n\n" +
      "- **GitHub**: Central for code hosting and reviews.\n" +
      "- Mattermost — Primary platform for integration.\n" +
      "- Neo4j: Memory layer database.\n";
    const out = parseOverviewBody(body);
    expect(out.tools).toHaveLength(3);
    expect(out.tools[0].name).toBe("GitHub");
    expect(out.tools[0].description).toContain("code hosting");
    expect(out.tools[1].name).toBe("Mattermost");
    expect(out.tools[2].name).toBe("Neo4j");
  });

  it("falls back to residual body when section parsing yields nothing", () => {
    const body =
      "## Key Contributors\n\nFreeform paragraph that isn't a bullet list.\n";
    const out = parseOverviewBody(body);
    expect(out.contributors).toHaveLength(0);
    // Section preserved in residual so WikiMarkdown can render it.
    expect(out.residualBody).toContain("## Key Contributors");
    expect(out.residualBody).toContain("Freeform paragraph");
  });

  it("drops 'Topics at a glance' / 'Recent momentum' / 'Key Highlights' sections from residual body", () => {
    // Defensive against legacy pages — the OVERVIEW_PROMPT no longer
    // emits these sections, but pages generated before the prompt
    // change still have them. The data is duplicated elsewhere on
    // the page (TopicCard grid, freshness chip, header chip row),
    // so re-rendering them here is wall-of-text noise.
    const body =
      "Intro.\n\n" +
      "## Topics at a glance\n\n- Topic A\n- Topic B\n\n" +
      "## Recent momentum\n\nGeneric prose about momentum.\n\n" +
      "## Key Highlights\n\n| Metric | Value |\n|---|---|\n| X | 1 |\n\n" +
      "## Useful Section\n\nKeep this one.\n";
    const out = parseOverviewBody(body);
    expect(out.residualBody).not.toContain("Topics at a glance");
    expect(out.residualBody).not.toContain("Recent momentum");
    expect(out.residualBody).not.toContain("Key Highlights");
    expect(out.residualBody).toContain("Useful Section");
  });

  it("strips trailing parenthetical roles from contributor lines", () => {
    const body =
      "## Key Contributors\n\n" +
      "- Alice: (lead) drives the security review for JWT [2]\n";
    const out = parseOverviewBody(body);
    const alice = out.contributors[0]?.entries[0];
    expect(alice?.name).toBe("Alice");
    expect(alice?.role).toBe("lead");
    expect(alice?.contribution).toContain("drives the security review");
    expect(alice?.citations).toEqual([2]);
  });
});
