import { describe, it } from "node:test";
import assert from "node:assert";
import { formatBlockKit } from "./formatter.js";

describe("formatBlockKit", () => {
  it("formats answer without citations", () => {
    const result = formatBlockKit("Hello world", [], "echo");
    assert.ok(result.includes("Hello world"));
    assert.ok(result.includes("Route: echo"));
    assert.ok(!result.includes("Sources:"));
  });

  it("formats answer with citations", () => {
    const citations = [
      { type: "fact", text: "The sky is blue" },
      { type: "graph", text: "Alice decided X" },
    ];
    const result = formatBlockKit("Answer here", citations, "semantic");
    assert.ok(result.includes("Answer here"));
    assert.ok(result.includes("[1] The sky is blue"));
    assert.ok(result.includes("[2] Alice decided X"));
    assert.ok(result.includes("Sources:"));
  });

  it("uses correct emoji for routes", () => {
    assert.ok(formatBlockKit("x", [], "semantic").includes(":brain:"));
    assert.ok(formatBlockKit("x", [], "graph").includes(":spider_web:"));
    assert.ok(formatBlockKit("x", [], "both").includes(":zap:"));
    assert.ok(formatBlockKit("x", [], "echo").includes(":loud_sound:"));
  });

  it("uses robot emoji for unknown routes", () => {
    const result = formatBlockKit("x", [], "unknown_route");
    assert.ok(result.includes(":robot_face:"));
  });
});
