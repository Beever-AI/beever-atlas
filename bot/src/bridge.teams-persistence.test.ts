import { afterEach, beforeEach, describe, it } from "node:test";
import assert from "node:assert";
import {
  recordTeamsConversation,
  seedTeamsKnownTeamIds,
  clearTeamsKnownTeamIdsForTest,
} from "./bridge.js";

// ── Helpers ──────────────────────────────────────────────────────────────────
// `recordTeamsConversation` fires a write-through POST to the backend whenever
// it observes a new aadGroupId. We don't want test cases to hit a real network
// — so we stub `fetch` with a recorder. Calls are captured for assertion; the
// stub resolves with a synthetic 204 No Content so the helper takes its
// success path.

interface CapturedFetch {
  url: string;
  init: { method?: string; headers?: Record<string, string>; body?: string };
}

const captured: CapturedFetch[] = [];
let originalFetch: typeof fetch;

function installFetchStub(status: number = 204): void {
  originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    captured.push({
      url: input.toString(),
      init: {
        method: init?.method,
        headers: init?.headers as Record<string, string> | undefined,
        body: init?.body ? String(init.body) : undefined,
      },
    });
    return new Response(null, { status });
  }) as typeof fetch;
}

function restoreFetch(): void {
  globalThis.fetch = originalFetch;
}

const VALID_AAD_GROUP_ID = "85e9fb0c-6cf9-4e94-9cc4-eb81ea6cd9de";
const ANOTHER_AAD_GROUP_ID = "11111111-2222-3333-4444-555555555555";
const CONN_ID = "test-conn-aabbccdd";
const OTHER_CONN_ID = "other-conn-eeff0011";

const installActivity = (aadGroupId: string, conversationId: string = "19:abcdef@thread.tacv2") => ({
  conversation: { id: conversationId, conversationType: "channel" },
  channelData: {
    team: { id: "team-internal-id", name: "Beever Atlas", aadGroupId },
    channel: { id: conversationId, name: "tech-discussion" },
  },
  serviceUrl: "https://smba.trafficmanager.net/amer/",
});

// ── seedTeamsKnownTeamIds ────────────────────────────────────────────────────

describe("seedTeamsKnownTeamIds", () => {
  beforeEach(() => {
    clearTeamsKnownTeamIdsForTest();
    captured.length = 0;
    installFetchStub();
  });
  afterEach(restoreFetch);

  it("hydrates the in-memory Map from a Mongo-supplied list", () => {
    seedTeamsKnownTeamIds(CONN_ID, [VALID_AAD_GROUP_ID, ANOTHER_AAD_GROUP_ID]);

    // After seeding, a subsequent webhook for the SAME id must NOT fire a
    // write-through — the value is already considered known.
    recordTeamsConversation(CONN_ID, installActivity(VALID_AAD_GROUP_ID));
    assert.strictEqual(
      captured.length,
      0,
      "seeded ids must dedup against incoming webhooks (no POST expected)",
    );
  });

  it("is a no-op for an empty list (preserves legacy cold-start scan path)", () => {
    seedTeamsKnownTeamIds(CONN_ID, []);
    // A subsequent observation must STILL fire the write-through because the
    // empty-seed call did not pre-populate any ids.
    recordTeamsConversation(CONN_ID, installActivity(VALID_AAD_GROUP_ID));
    assert.strictEqual(captured.length, 1);
  });

  it("scopes seeded ids per-connection (no cross-connection leak)", () => {
    seedTeamsKnownTeamIds(CONN_ID, [VALID_AAD_GROUP_ID]);

    // A different connection observing the same team-id must fire its own
    // write-through; ids are not shared across connections (Redis cache is
    // global but Mongo persistence is per-row).
    recordTeamsConversation(OTHER_CONN_ID, installActivity(VALID_AAD_GROUP_ID));
    assert.strictEqual(captured.length, 1);
    assert.ok(
      captured[0].url.includes(encodeURIComponent(OTHER_CONN_ID)),
      "write-through URL must target the OTHER connection",
    );
  });

  it("is idempotent across repeated calls (adapter rebuild path)", () => {
    seedTeamsKnownTeamIds(CONN_ID, [VALID_AAD_GROUP_ID]);
    seedTeamsKnownTeamIds(CONN_ID, [VALID_AAD_GROUP_ID]);
    seedTeamsKnownTeamIds(CONN_ID, [VALID_AAD_GROUP_ID, ANOTHER_AAD_GROUP_ID]);

    // A webhook for either id is now a no-op write-through.
    recordTeamsConversation(CONN_ID, installActivity(VALID_AAD_GROUP_ID));
    recordTeamsConversation(CONN_ID, installActivity(ANOTHER_AAD_GROUP_ID));
    assert.strictEqual(captured.length, 0);
  });
});

// ── Write-through on observed aadGroupId ─────────────────────────────────────

describe("recordTeamsConversation write-through", () => {
  beforeEach(() => {
    clearTeamsKnownTeamIdsForTest();
    captured.length = 0;
    installFetchStub();
  });
  afterEach(restoreFetch);

  it("POSTs to the backend the first time a new aadGroupId is observed", () => {
    recordTeamsConversation(CONN_ID, installActivity(VALID_AAD_GROUP_ID));

    assert.strictEqual(captured.length, 1);
    const { url, init } = captured[0];
    assert.ok(
      url.includes(`/api/internal/connections/${encodeURIComponent(CONN_ID)}/teams-known-team-ids`),
      `unexpected URL: ${url}`,
    );
    assert.strictEqual(init.method, "POST");
    assert.deepStrictEqual(JSON.parse(init.body || "{}"), {
      aad_group_id: VALID_AAD_GROUP_ID,
    });
  });

  it("dedups subsequent observations of the same id (no second POST)", () => {
    recordTeamsConversation(CONN_ID, installActivity(VALID_AAD_GROUP_ID));
    recordTeamsConversation(CONN_ID, installActivity(VALID_AAD_GROUP_ID));
    recordTeamsConversation(CONN_ID, installActivity(VALID_AAD_GROUP_ID));
    assert.strictEqual(captured.length, 1, "duplicate observations must not refire");
  });

  it("fires a separate POST for a DIFFERENT aadGroupId on the same connection", () => {
    recordTeamsConversation(CONN_ID, installActivity(VALID_AAD_GROUP_ID));
    recordTeamsConversation(CONN_ID, installActivity(ANOTHER_AAD_GROUP_ID));
    assert.strictEqual(captured.length, 2);
    const ids = captured.map((c) => JSON.parse(c.init.body || "{}").aad_group_id).sort();
    assert.deepStrictEqual(ids, [ANOTHER_AAD_GROUP_ID, VALID_AAD_GROUP_ID].sort());
  });

  it("rejects malformed aadGroupId without firing a POST", () => {
    const activity = installActivity("not-a-guid-at-all");
    recordTeamsConversation(CONN_ID, activity);
    assert.strictEqual(captured.length, 0, "non-GUID aadGroupId must be filtered client-side");
  });

  it("does not fire when aadGroupId is absent (regular channel-message activity)", () => {
    const activity = {
      conversation: { id: "19:abcdef@thread.tacv2", conversationType: "channel" },
      channelData: {
        team: { id: "team-internal-id", name: "Beever Atlas" }, // no aadGroupId
        channel: { id: "19:abcdef@thread.tacv2", name: "tech-discussion" },
      },
    };
    recordTeamsConversation(CONN_ID, activity);
    assert.strictEqual(captured.length, 0);
  });
});
