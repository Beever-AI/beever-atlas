/**
 * Tests for ``truncateAtSentence``.
 *
 * Coverage:
 *  (a) text under maxLength → returned unchanged
 *  (b) sentence boundary inside budget → cuts at terminator (no ellipsis)
 *  (c) word boundary fallback when no sentence terminator in window
 *  (d) hard truncate with ellipsis when no whitespace inside budget
 *  (e) handles ?, ! terminators (not just .)
 *  (f) empty / null inputs return ""
 */

import { describe, it, expect } from "vitest";
import { truncateAtSentence } from "@/lib/textTruncate";

describe("truncateAtSentence", () => {
  it("returns the input unchanged when shorter than maxLength", () => {
    expect(truncateAtSentence("Short text.", 100)).toBe("Short text.");
  });

  it("returns input unchanged when exactly at the budget length", () => {
    const exact = "A".repeat(50);
    expect(truncateAtSentence(exact, 50)).toBe(exact);
  });

  it("cuts at the latest sentence terminator inside the budget", () => {
    const text =
      "First sentence. Second sentence is here. The third sentence rambles on indefinitely with details.";
    const result = truncateAtSentence(text, 50);
    // 'First sentence. Second sentence is here.' = 40 chars (within 50).
    // The third sentence terminator falls beyond 50, so we cut at the
    // closest terminator inside the window.
    expect(result).toBe("First sentence. Second sentence is here.");
    // Sentence-boundary cuts do NOT append an ellipsis.
    expect(result).not.toContain("…");
  });

  it("falls back to word boundary when no sentence terminator in window", () => {
    const text =
      "this is a long phrase without any sentence terminators inside the budget window at all";
    const result = truncateAtSentence(text, 30);
    // Cut should land at a space boundary, not mid-word.
    expect(result.endsWith("…")).toBe(true);
    // No word should be split mid-token (last char before … should not be
    // an alphanumeric character that's part of a longer source word).
    const trimmed = result.replace("…", "").trimEnd();
    // The trimmed result must be a prefix of the source text (verifies no
    // mid-word cut, just whitespace-aligned).
    expect(text.startsWith(trimmed)).toBe(true);
    // The character right after the trimmed prefix must be a space (the
    // boundary we cut on) — proving we landed on a word edge.
    expect(text.charAt(trimmed.length)).toBe(" ");
  });

  it("hard-truncates with ellipsis when no whitespace inside budget", () => {
    // Single very long token: hits the last-resort branch.
    const text = "supercalifragilisticexpialidocious-and-more-letters-too";
    const result = truncateAtSentence(text, 10);
    expect(result).toBe(text.slice(0, 10) + "…");
  });

  it("recognises '?' as a sentence terminator", () => {
    const text =
      "How does this work? It's actually quite simple under the hood.";
    const result = truncateAtSentence(text, 25);
    expect(result).toBe("How does this work?");
  });

  it("recognises '!' as a sentence terminator", () => {
    const text =
      "Watch out for this edge case! It can bite you when you least expect.";
    const result = truncateAtSentence(text, 30);
    expect(result).toBe("Watch out for this edge case!");
  });

  it("returns empty string for null / undefined / empty input", () => {
    expect(truncateAtSentence(null, 50)).toBe("");
    expect(truncateAtSentence(undefined, 50)).toBe("");
    expect(truncateAtSentence("", 50)).toBe("");
  });

  it("does not cut at a sentence terminator that falls before halfway", () => {
    // Terminator at position 5 with maxLength=100 → not enough text
    // would survive (10% of budget). Should fall back to word boundary
    // or hard-cut, not return the tiny prefix.
    const text =
      "Yes. Followed by an unrelated long stream of words that goes on and on without clear punctuation here";
    const result = truncateAtSentence(text, 100);
    // The function returns the whole input when shorter than budget.
    expect(result.length).toBeGreaterThan(50);
  });
});
