/**
 * Tests for ``buildWikiPath`` and ``preserveQueryParams`` — the small
 * navigation helpers that back the wiki tab's path-based routing.
 *
 * The helpers are intentionally tiny so the tests focus on the
 * contract callers depend on: URL shape, slug encoding, and the empty
 * vs non-empty search-string return.
 */
import { describe, it, expect } from "vitest";
import { buildWikiPath, preserveQueryParams } from "../wikiNav";

describe("buildWikiPath", () => {
  it("returns /channels/{id}/wiki when slug is undefined", () => {
    expect(buildWikiPath("c1")).toBe("/channels/c1/wiki");
  });

  it("returns /channels/{id}/wiki when slug is empty string", () => {
    expect(buildWikiPath("c1", "")).toBe("/channels/c1/wiki");
  });

  it("returns /channels/{id}/wiki/{slug} when slug is present", () => {
    expect(buildWikiPath("c1", "topic-auth")).toBe(
      "/channels/c1/wiki/topic-auth",
    );
  });

  it("URL-encodes special characters in the slug", () => {
    // Space + slash + hash → all need percent-encoding so they don't
    // confuse the path parser.
    expect(buildWikiPath("c1", "topic with space")).toBe(
      "/channels/c1/wiki/topic%20with%20space",
    );
    expect(buildWikiPath("c1", "topic/with/slash")).toBe(
      "/channels/c1/wiki/topic%2Fwith%2Fslash",
    );
    expect(buildWikiPath("c1", "topic#hash")).toBe(
      "/channels/c1/wiki/topic%23hash",
    );
  });
});

describe("preserveQueryParams", () => {
  it("returns empty string when input has no params", () => {
    const params = new URLSearchParams();
    expect(preserveQueryParams(params)).toBe("");
  });

  it("returns ?key=value when input has params and nothing is dropped", () => {
    const params = new URLSearchParams();
    params.set("view", "graph");
    params.set("lang", "ja");
    expect(preserveQueryParams(params)).toBe("?view=graph&lang=ja");
  });

  it("drops listed param names and keeps the rest", () => {
    const params = new URLSearchParams();
    params.set("view", "graph");
    params.set("page", "topic-x");
    params.set("lang", "ja");
    expect(preserveQueryParams(params, ["page"])).toBe("?view=graph&lang=ja");
  });

  it("returns empty string when dropping leaves no params", () => {
    const params = new URLSearchParams();
    params.set("page", "topic-x");
    expect(preserveQueryParams(params, ["page"])).toBe("");
  });

  it("does not mutate the input URLSearchParams", () => {
    const params = new URLSearchParams();
    params.set("view", "graph");
    params.set("page", "topic-x");
    preserveQueryParams(params, ["page"]);
    // Original still has both keys.
    expect(params.get("page")).toBe("topic-x");
    expect(params.get("view")).toBe("graph");
  });
});
