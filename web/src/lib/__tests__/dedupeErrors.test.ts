import { describe, it, expect } from "vitest";
import { dedupeErrors, formatDedupedErrors } from "@/lib/dedupeErrors";

describe("dedupeErrors", () => {
  it("returns an empty array for undefined input", () => {
    expect(dedupeErrors(undefined)).toEqual([]);
  });

  it("returns an empty array for an empty list", () => {
    expect(dedupeErrors([])).toEqual([]);
  });

  it("filters out null, undefined, and whitespace-only entries", () => {
    expect(dedupeErrors([null, undefined, "   ", ""])).toEqual([]);
  });

  it("collapses identical messages and counts them", () => {
    const result = dedupeErrors([
      "503 UNAVAILABLE",
      "503 UNAVAILABLE",
      "503 UNAVAILABLE",
    ]);
    expect(result).toEqual([{ message: "503 UNAVAILABLE", count: 3 }]);
  });

  it("preserves first-occurrence order across distinct messages", () => {
    const result = dedupeErrors([
      "503 UNAVAILABLE",
      "Quota exceeded",
      "503 UNAVAILABLE",
      "Quota exceeded",
      "503 UNAVAILABLE",
    ]);
    expect(result).toEqual([
      { message: "503 UNAVAILABLE", count: 3 },
      { message: "Quota exceeded", count: 2 },
    ]);
  });

  it("trims whitespace before comparison", () => {
    const result = dedupeErrors(["  503 UNAVAILABLE  ", "503 UNAVAILABLE"]);
    expect(result).toEqual([{ message: "503 UNAVAILABLE", count: 2 }]);
  });
});

describe("formatDedupedErrors", () => {
  it("renders a single-count message without the count suffix", () => {
    expect(
      formatDedupedErrors([{ message: "Network error", count: 1 }]),
    ).toBe("Network error");
  });

  it("renders a multi-count message with (×N batches) suffix", () => {
    expect(
      formatDedupedErrors([{ message: "503 UNAVAILABLE", count: 12 }]),
    ).toBe("503 UNAVAILABLE (×12 batches)");
  });

  it("joins multiple deduped entries with semicolons", () => {
    expect(
      formatDedupedErrors([
        { message: "503 UNAVAILABLE", count: 3 },
        { message: "Quota exceeded", count: 1 },
      ]),
    ).toBe("503 UNAVAILABLE (×3 batches); Quota exceeded");
  });

  it("returns empty string when there are no errors", () => {
    expect(formatDedupedErrors([])).toBe("");
  });
});
