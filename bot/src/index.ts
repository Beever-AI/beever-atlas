import { config } from "dotenv";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { createServer, IncomingMessage, ServerResponse } from "node:http";

// Load .env from project root (one level up from bot/)
config({ path: resolve(import.meta.dirname, "../../.env") });
import { Chat } from "chat";
import { renderResponse } from "./renderer.js";
import { fetchSSEWithRetry } from "./sse-client.js";
import { extractChannelId, extractPlatform, extractThreadId, hasThreadRoot } from "./thread-id.js";
import {
  decideShouldRespond,
  decideSubscribedThreadActionWithLookup,
  isBroadcast,
} from "./trigger.js";
import { stripMention } from "./mentions.js";
import { ParticipantCache } from "./participant-cache.js";
import { logReplySent, type ReplySurface } from "./reply-metrics.js";
import { deriveSessionId } from "./session-id.js";
import { RateLimiter } from "./rate-limiter.js";
import type { AskResult } from "./types.js";
import { registerBridgeRoutes, recordTelegramChat, recordTeamsConversation, warmTeamsGraphToken, seedTeamsKnownTeamIds } from "./bridge.js";
import { jsonResponse, readBody, MAX_BODY_SIZE, BodyTooLargeError, safeErrorMessage } from "./http-utils.js";
import { ChatManager } from "./chat-manager.js";
import { DiscordGatewaySupervisor } from "./discord-gateway.js";

import { WebhookBuffer } from "./webhook-buffer.js";
import { validateEnv } from "./validate-env.js";

// ── Environment validation ──────────────────────────────────────────────────

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const REDIS_URL = process.env.REDIS_URL || "redis://localhost:6379";
const PORT = parseInt(process.env.BOT_PORT || "3001", 10);

// Reply-feature tuning (all env-overridable for safe rollback):
//  - ASK_TIMEOUT_MS bounds a single /ask call (retries share the budget).
//  - HUMAN_QUIET_THRESHOLD: at/above this many humans in a thread, the bot
//    withdraws from non-mention follow-ups (anti-spam). Default 2 = it stays
//    chatty only while effectively 1:1 with a single human.
//  - TRIGGER_REDESIGN: master switch for the new subscribed-thread gating;
//    set BOT_TRIGGER_REDESIGN=off to revert to legacy "answer everything".
const ASK_TIMEOUT_MS = parseInt(process.env.BOT_ASK_TIMEOUT_MS || "45000", 10);
const HUMAN_QUIET_THRESHOLD = parseInt(process.env.BOT_HUMAN_QUIET_THRESHOLD || "2", 10);
const TRIGGER_REDESIGN = (process.env.BOT_TRIGGER_REDESIGN || "on").toLowerCase() !== "off";
//  - DM_ENABLED: answer direct messages (1:1 Q&A). Set BOT_DM_ENABLED=off to disable.
//  - PARTICIPANT_CACHE_TTL_MS: cache a thread's human count to avoid a
//    getParticipants() round-trip per non-mention message. 0 disables the cache.
const DM_ENABLED = (process.env.BOT_DM_ENABLED || "on").toLowerCase() !== "off";
const PARTICIPANT_CACHE_TTL_MS = parseInt(process.env.BOT_PARTICIPANT_CACHE_TTL_MS || "30000", 10);
//  - RATELIMIT_PER_MIN: max questions answered per (platform,channel,user) per
//    minute before a one-time "give me a moment" notice and silent drop.
const RATE_LIMIT_PER_MIN = parseInt(process.env.BOT_RATELIMIT_PER_MIN || "12", 10);
const RATE_WINDOW_MS = 60_000;

const participantCache = new ParticipantCache(PARTICIPANT_CACHE_TTL_MS);
const rateLimiter = new RateLimiter(RATE_LIMIT_PER_MIN, RATE_WINDOW_MS);
// A second 1-per-window limiter so the throttle notice itself can't spam.
const noticeLimiter = new RateLimiter(1, RATE_WINDOW_MS);

// Issue #53 — validateEnv lives in ./validate-env.ts; it's WARN-only and
// never gates startup (platform-specific creds are loaded from the backend
// database at runtime). See that module for the full check list.

// ── Handler registration ─────────────────────────────────────────────────────

function registerHandlers(bot: Chat): void {
  // Handler: user @mentions the bot in a not-yet-subscribed thread.
  bot.onNewMention(async (thread, message) => {
    // Log length, not content — message text can carry PII/secrets.
    console.log(`[@mention] (${(message.text || "").length} chars) from ${thread.id}`);
    // Never answer our own message or another bot — the latter prevents
    // bot-to-bot @mention loops (e.g. a workflow bot pinging Beever).
    if (message.author?.isMe === true || message.author?.isBot === true) return;
    await thread.subscribe();

    const platform = extractPlatform(thread.id);
    const question = stripMention(message.text || "", platform);
    // Addressing & intent gate: only speak when actually asked something. Bare
    // mention → nudge; an announcement (@channel/@here/@everyone) or a pleasantry
    // that happens to tag the bot → stay quiet.
    const decision = decideShouldRespond({
      text: question,
      broadcast: isBroadcast(message.text || ""),
      isMention: true,
      surface: "mention",
    });
    if (decision === "prompt") {
      await thread.post("Please ask me a question! For example: @beever what is our tech stack?");
      return;
    }
    if (decision === "skip") return;

    if (!(await passesRateLimit(thread, message))) return;
    // P2 — key on the THREAD (isThreaded=true), same as onSubscribedMessage.
    // On every supported platform `thread.id` is the STABLE thread ROOT: Slack
    // encodes it as `slack:<channel>:<threadTs>` where threadTs is
    // `event.thread_ts || event.ts`, so a root @mention (no thread_ts) and all
    // its replies (thread_ts = root ts) share one id. Keying both handlers on
    // the thread is what makes the root @mention and its follow-ups resume ONE
    // backend session — the old `false` here put the root on a separate
    // idle-window key, so memory never carried into the thread. Fall back to the
    // loose idle-window key only for a degenerate id with no thread segment.
    await answerInThread(thread, question, "mention", message.author, hasThreadRoot(thread.id));
  });

  // Handler: follow-up messages in subscribed threads. The SDK routes EVERY
  // message here (including the bot's own replies and non-mention chatter), so
  // we gate before answering — see trigger.ts for the rules.
  bot.onSubscribedMessage(async (thread, message) => {
    // Log length, not content — message text can carry PII/secrets.
    console.log(`[subscribed] (${(message.text || "").length} chars) in ${thread.id}`);

    // Always skip self/other-bots, even when the redesign flag is off — this is
    // the minimum guard that prevents the reply-storm, so the legacy fallback
    // path stays safe to roll back to.
    if (message.author?.isMe === true || message.author?.isBot === true) return;

    const platform = extractPlatform(thread.id);
    const question = stripMention(message.text || "", platform);
    if (!question.trim()) return;

    if (TRIGGER_REDESIGN) {
      const action = await decideSubscribedThreadAction(thread, message);
      if (action === "skip") return;
      if (action === "unsubscribe") {
        // Thread became a multi-human conversation — withdraw and stay quiet.
        try {
          await thread.unsubscribe();
        } catch (err) {
          console.error("Failed to unsubscribe from busy thread:", safeErrorMessage(err));
        }
        return;
      }
    }

    // Addressing & intent gate (runs AFTER the stay-subscribed decision, so a
    // busy thread still triggers unsubscribe): only answer clear questions/
    // requests, or statements that directly @mention the bot. Broadcasts/
    // announcements, pleasantries, and passive non-mention chatter → stay quiet.
    const decision = decideShouldRespond({
      text: question,
      broadcast: isBroadcast(message.text || ""),
      isMention: message.isMention === true,
      surface: "follow-up",
    });
    if (decision !== "respond") return;

    if (!(await passesRateLimit(thread, message))) return;
    await answerInThread(thread, question, "follow-up", message.author);
  });

  // Handler: direct messages — a private 1:1 Q&A surface. DMs route through the
  // same webhook dispatch, so no manifest config or new endpoint is needed.
  if (DM_ENABLED) {
    bot.onDirectMessage(async (thread, message) => {
      console.log(`[dm] (from ${thread.id})`);
      if (message.author?.isMe === true || message.author?.isBot === true) return;

      const platform = extractPlatform(thread.id);
      const question = stripMention(message.text || "", platform);
      // DM is a direct 1:1 surface: answer questions and statements, nudge on an
      // empty message, but still skip a bare pleasantry ("thanks").
      const decision = decideShouldRespond({
        text: question,
        broadcast: false,
        isMention: true,
        surface: "dm",
      });
      if (decision === "prompt") {
        await thread.post("Ask me anything about this workspace's knowledge.");
        return;
      }
      if (decision === "skip") return;
      if (!(await passesRateLimit(thread, message))) return;
      await answerInThread(thread, question, "dm", message.author);
    });
  }
}

/**
 * Minimal thread surface the reply path needs. Structural (not the full SDK
 * `Thread`) so unit tests can pass a stub. `post` accepts `{ markdown }` so the
 * SDK converts canonical CommonMark to each platform's native format;
 * `postEphemeral` / `startTyping` are optional and feature-detected at runtime.
 */
export type PostableThread = {
  id: string;
  post(message: string | { markdown: string }): Promise<unknown>;
  postEphemeral?(
    user: unknown,
    message: string | { markdown: string },
    options: { fallbackToDM?: boolean },
  ): Promise<unknown>;
  startTyping?(status?: string): Promise<void>;
};

/**
 * Post a transient notice (error / rate-limit). Prefers an EPHEMERAL message so
 * it isn't a permanent channel artifact for everyone — Slack/Google Chat show
 * it only to `author`; Discord/Teams fall back to a DM. Any platform without
 * ephemeral support (or a failing ephemeral call) degrades to a normal thread
 * post so the user still sees it. Best-effort: never throws.
 */
export async function postNotice(thread: PostableThread, author: unknown, text: string): Promise<void> {
  if (author != null && typeof thread.postEphemeral === "function") {
    try {
      await thread.postEphemeral(author, text, { fallbackToDM: true });
      return;
    } catch (err) {
      console.error("Ephemeral notice failed; falling back to post:", safeErrorMessage(err));
    }
  }
  await thread.post(text);
}

/**
 * Inbound rate gate, keyed per (platform, channel, user) so one user can't
 * exhaust a shared channel's budget. On the first block within a window it posts
 * a single friendly notice (the notice itself is rate-limited), then drops
 * silently. Returns true when the request may proceed.
 */
async function passesRateLimit(
  thread: PostableThread,
  message: { author?: { userId?: string } },
): Promise<boolean> {
  const key = `${extractPlatform(thread.id)}:${extractChannelId(thread.id)}:${message.author?.userId || "unknown"}`;
  const decision = rateLimiter.check(key);
  if (decision.allowed) return true;
  if (noticeLimiter.check(key).allowed) {
    const secs = Math.max(1, Math.ceil(decision.retryAfterMs / 1000));
    try {
      await postNotice(
        thread,
        message.author,
        `You're asking a lot — give me a moment 🙂 (try again in ~${secs}s)`,
      );
    } catch (err) {
      console.error("Failed to post rate-limit notice:", safeErrorMessage(err));
    }
  }
  return false;
}

type ThreadForParticipants = {
  id: string;
  getParticipants(): Promise<Array<{ isMe: boolean; isBot?: boolean | "unknown" }>>;
};

/**
 * Decide what to do with a subscribed-thread message. Delegates the rules to the
 * pure `decideSubscribedThreadActionWithLookup`, injecting a cached human-count
 * lookup so a busy thread doesn't trigger a getParticipants() call per message.
 */
async function decideSubscribedThreadAction(
  thread: ThreadForParticipants,
  message: { isMention?: boolean; author?: { isMe?: boolean; isBot?: boolean | "unknown" } },
): Promise<"answer" | "skip" | "unsubscribe"> {
  return decideSubscribedThreadActionWithLookup(
    {
      isMe: message.author?.isMe === true,
      isBot: message.author?.isBot,
      isMention: message.isMention === true,
      quietThreshold: HUMAN_QUIET_THRESHOLD,
    },
    () => getHumanCount(thread),
  );
}

/** Human participant count for a thread, served from a short TTL cache. */
async function getHumanCount(thread: ThreadForParticipants): Promise<number | undefined> {
  const cached = participantCache.get(thread.id);
  if (cached !== undefined) return cached;
  try {
    const participants = await thread.getParticipants();
    // Count only humans — exclude self and other bots so a single human + a
    // helper bot doesn't trip the multi-human "go quiet" threshold.
    const humanCount = participants.filter((p) => p.isMe !== true && p.isBot !== true).length;
    participantCache.set(thread.id, humanCount);
    return humanCount;
  } catch (err) {
    console.error("getParticipants failed; defaulting to answer:", safeErrorMessage(err));
    return undefined;
  }
}

/** Ask the backend and post the rendered reply, swallowing errors into a friendly notice. */
export async function answerInThread(
  thread: PostableThread,
  question: string,
  surface: ReplySurface,
  author?: unknown,
  isThreaded: boolean = true,
): Promise<void> {
  const platform = extractPlatform(thread.id);
  const startedAt = Date.now();
  const userId = (author as { userId?: string } | undefined)?.userId || "unknown";
  const channelId = extractChannelId(thread.id);
  const threadId = extractThreadId(thread.id);
  try {
    // Show a typing/thinking indicator while the backend streams so a
    // multi-second answer doesn't feel unresponsive. Feature-detected and
    // fire-and-forget — a no-op on adapters that don't support it, and posting
    // the reply clears it. Never let a failing indicator break the reply.
    if (typeof thread.startTyping === "function") {
      thread.startTyping().catch(() => {});
    }
    // Stable session id → backend resumes the prior exchange so follow-ups
    // remember context. Threaded replies key on the thread; loose top-level
    // @mentions key on (user, channel) within an idle window. See session-id.ts
    // for the security rationale.
    const result = await askBackend(
      channelId,
      question,
      deriveSessionId(threadId, userId, channelId, isThreaded),
      userId,
    );
    // Post as `{ markdown }`: the renderer emits canonical CommonMark and the
    // chat SDK converts it to each platform's native format (Slack Block Kit,
    // Teams Adaptive Card, …). Posting a raw string instead made `##`/`**`
    // render literally on Slack/Teams/Telegram.
    await thread.post({ markdown: renderResponse(result, platform) });
    logReplySent({
      surface,
      platform,
      route: result.route,
      latencyMs: Date.now() - startedAt,
      isEmpty: result.isEmpty,
      citationCount: result.citations.length,
      followUpCount: result.followUps?.length ?? 0,
    });
  } catch (err) {
    console.error(`Error processing ${surface}:`, safeErrorMessage(err));
    // A failed reply must not throw out of the handler: that would make the
    // webhook return 5xx and skip conversation recording (serviceUrl), which
    // breaks later message fetches even though the activity was valid. The
    // error goes out ephemeral (DM fallback) so it doesn't clutter the channel.
    try {
      await postNotice(
        thread,
        author,
        "Sorry, I encountered an error processing your question. Please try again.",
      );
    } catch (postErr) {
      console.error("Failed to deliver error reply:", safeErrorMessage(postErr));
    }
  }
}

// ── Helpers ─────────────────────────────────────────────────────────────────
// `AskResult` and the thread-id parsers now live in shared modules so pure
// consumers (sse-client, renderer, tests) don't import the server bootstrap.

export type { AskResult } from "./types.js";

function backendApiKey(): string {
  const raw = process.env.BEEVER_API_KEYS || "";
  return raw.split(",").map((k) => k.trim()).find(Boolean) || "";
}

async function askBackend(
  channelId: string,
  question: string,
  sessionId: string,
  userId: string,
): Promise<AskResult> {
  // Encode the channel id so a value with slashes/`..` can't escape the route
  // path and reach an unintended backend endpoint.
  const url = `${BACKEND_URL}/api/channels/${encodeURIComponent(channelId)}/ask`;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const apiKey = backendApiKey();
  if (apiKey) {
    headers["Authorization"] = `Bearer ${apiKey}`;
  }
  // Bound the whole call (the timeout budget is shared across retries) and let
  // fetchSSEWithRetry absorb transient 5xx / network blips with backoff so a
  // slow or flapping backend doesn't surface a hard error to the user.
  return fetchSSEWithRetry(
    url,
    { method: "POST", headers, body: JSON.stringify({ question, session_id: sessionId, user_id: userId }) },
    // Pass the channel id so the SSE client can scrub a leaked raw channel id
    // from the answer and use it to collapse an `(id)`-variant near-duplicate.
    { maxAttempts: 3, signal: AbortSignal.timeout(ASK_TIMEOUT_MS), channelId },
  );
}

// ── Backend health check ────────────────────────────────────────────────────

async function isBackendHealthy(): Promise<boolean> {
  try {
    const response = await fetch(`${BACKEND_URL}/api/health`, { signal: AbortSignal.timeout(3000) });
    return response.ok;
  } catch {
    return false;
  }
}

// ── Startup sync with retry ──────────────────────────────────────────────────

/**
 * Fetches connections from the backend and registers them. Returns true if
 * at least one connection was synced, false otherwise.
 */
async function syncConnectionsFromBackend(chatManager: ChatManager, label: string): Promise<boolean> {
  const BRIDGE_API_KEY = process.env.BRIDGE_API_KEY || "";
  const headers: Record<string, string> = {};
  if (BRIDGE_API_KEY) {
    headers["Authorization"] = `Bearer ${BRIDGE_API_KEY}`;
  }

  const response = await fetch(`${BACKEND_URL}/api/internal/connections/credentials`, { headers });
  if (!response.ok) {
    throw new Error(`Backend returned ${response.status}`);
  }

  const connections = await response.json() as Array<{
    connection_id?: string;
    platform: string;
    credentials: Record<string, string>;
    status: string;
    // Mongo-persisted AAD group ids for Teams connections; empty for other
    // platforms. Used to seed `teamsKnownTeamIds` so listChannels works on
    // the first call without requiring a webhook or warm Redis cache. See
    // `seedTeamsKnownTeamIds` in bridge.ts.
    teams_known_team_ids?: string[];
  }>;

  if (connections.length === 0) {
    if (!loggedNoConnections) {
      console.log(`${label}: no connections found in backend — will retry silently`);
      loggedNoConnections = true;
    }
    return false;
  }

  // Connections appeared — reset the flag so removal is logged next time
  loggedNoConnections = false;

  // Build a fingerprint from incoming connections to detect changes
  const incomingKeys = connections
    .map((c) => `${c.platform}:${c.connection_id || c.platform}`)
    .sort()
    .join(",");

  if (incomingKeys === chatManager.adapterFingerprint()) {
    console.log(`${label}: adapters unchanged, skipping rebuild`);
    return true;
  }

  for (const conn of connections) {
    // Normalize credential keys: backend stores snake_case, ChatSDK expects camelCase
    const normalizedCreds: Record<string, string> = {};
    for (const [key, value] of Object.entries(conn.credentials)) {
      const camelKey = key.replace(/_([a-z])/g, (_: string, c: string) => c.toUpperCase());
      normalizedCreds[camelKey] = String(value);
    }
    console.log(`${label}: registering ${conn.platform} adapter (connection: ${conn.connection_id || "legacy"})`);
    await chatManager.register(conn.platform, normalizedCreds, conn.connection_id);

    // Hydrate the in-memory Teams team-id Map from the durable Mongo record
    // so the first `listChannels` call after a bot restart (or Redis cache
    // wipe) returns the workspace's channels without needing an inbound
    // webhook to reseed identity. This is the parity path with Slack/
    // Discord/Mattermost (whose tokens already live in Mongo).
    if (conn.platform === "teams" && conn.connection_id && conn.teams_known_team_ids?.length) {
      seedTeamsKnownTeamIds(conn.connection_id, conn.teams_known_team_ids);
      console.log(
        `${label}: seeded ${conn.teams_known_team_ids.length} known team id(s) for Teams connection ${conn.connection_id}`,
      );
    }
  }

  console.log(`${label}: loaded ${connections.length} connection(s) from backend`);
  return true;
}

async function loadConnectionsFromBackend(chatManager: ChatManager): Promise<void> {
  // RES-286 — compressed from [1, 2, 4, 8, 16]s (31 s worst case) so that a
  // bot restart only blocks `/bridge/...` calls for ~12 s, not ~31 s. The
  // backend health check + Redis are typically up within ~3 s; we keep five
  // retries for genuinely slow boots but cap the trailing waits at 4 s each.
  const delays = [500, 1000, 2000, 4000, 4000];

  for (let attempt = 0; attempt < delays.length; attempt++) {
    try {
      // Health-aware: check backend availability before fetching connections
      const healthy = await isBackendHealthy();
      if (!healthy) {
        throw new Error("Backend health check failed");
      }

      const synced = await syncConnectionsFromBackend(chatManager, "Startup sync");
      if (synced || chatManager.adapterCount() > 0) return;
      // No connections found — still a successful call, just nothing to load
      return;
    } catch (err) {
      const isLastAttempt = attempt === delays.length - 1;
      if (isLastAttempt) {
        console.warn(`Startup sync: all ${delays.length} attempts failed. Falling back to .env credentials.`);
        await fallbackToEnvCredentials(chatManager);
      } else {
        const waitMs = delays[attempt];
        console.warn(`Startup sync: attempt ${attempt + 1} failed (${safeErrorMessage(err)}), retrying in ${waitMs}ms...`);
        await new Promise((r) => setTimeout(r, waitMs));
      }
    }
  }
}

async function registerTeamsFromEnvIfPresent(
  chatManager: ChatManager,
  logPrefix: string,
): Promise<boolean> {
  const teamsAppId = process.env.TEAMS_APP_ID;
  const teamsAppPassword = process.env.TEAMS_APP_PASSWORD;
  if (!teamsAppId || !teamsAppPassword) return false;

  const teamsAppTenantId = process.env.TEAMS_APP_TENANT_ID;
  const appType = teamsAppTenantId ? "SingleTenant" : "MultiTenant";
  console.log(`${logPrefix}: registering Teams adapter from .env credentials (${appType})`);
  await chatManager.register("teams", {
    appId: teamsAppId,
    appPassword: teamsAppPassword,
    ...(teamsAppTenantId ? { appTenantId: teamsAppTenantId, appType } : {}),
  });
  return true;
}

async function fallbackToEnvCredentials(chatManager: ChatManager): Promise<void> {
  let registered = 0;

  // Slack
  const slackToken = process.env.SLACK_BOT_TOKEN;
  const slackSecret = process.env.SLACK_SIGNING_SECRET;
  if (slackToken && slackSecret) {
    console.log("Env fallback: registering Slack adapter from .env credentials");
    await chatManager.register("slack", { botToken: slackToken, signingSecret: slackSecret });
    registered++;
  }

  // Discord
  const discordToken = process.env.DISCORD_BOT_TOKEN;
  const discordPublicKey = process.env.DISCORD_PUBLIC_KEY;
  const discordAppId = process.env.DISCORD_APPLICATION_ID;
  if (discordToken && discordPublicKey && discordAppId) {
    console.log("Env fallback: registering Discord adapter from .env credentials");
    await chatManager.register("discord", {
      botToken: discordToken,
      publicKey: discordPublicKey,
      applicationId: discordAppId,
    });
    registered++;
  }

  // Teams
  if (await registerTeamsFromEnvIfPresent(chatManager, "Env fallback")) {
    registered++;
  }

  // Telegram
  const telegramToken = process.env.TELEGRAM_BOT_TOKEN;
  if (telegramToken) {
    console.log("Env fallback: registering Telegram adapter from .env credentials");
    await chatManager.register("telegram", { botToken: telegramToken });
    registered++;
  }

  // Mattermost
  const mmBaseUrl = process.env.MATTERMOST_BASE_URL;
  const mmBotToken = process.env.MATTERMOST_BOT_TOKEN;
  if (mmBaseUrl && mmBotToken) {
    console.log("Env fallback: registering Mattermost adapter from .env credentials");
    await chatManager.register("mattermost", { baseUrl: mmBaseUrl, botToken: mmBotToken });
    registered++;
  }

  if (registered === 0) {
    console.warn("Env fallback: no .env credentials available — bot starting without adapters");
  }
}

// ── Periodic background sync ────────────────────────────────────────────────

const SYNC_INTERVAL_MS = 60_000;
let backgroundSyncTimer: ReturnType<typeof setInterval> | null = null;
let backgroundSyncRunning = false;
let loggedNoConnections = false;

function startBackgroundSync(chatManager: ChatManager): void {
  if (backgroundSyncTimer) return;

  backgroundSyncTimer = setInterval(async () => {
    // Skip if a sync is already in progress
    if (backgroundSyncRunning) return;

    // Only sync when the bot has no adapters (self-healing) or backend may have
    // new connections. Always attempt when adapter count is 0.
    if (chatManager.adapterCount() > 0) {
      // Still attempt periodically to pick up new connections, but only if
      // the backend is healthy (cheap check avoids unnecessary errors in logs)
      const healthy = await isBackendHealthy();
      if (!healthy) return;
    }

    backgroundSyncRunning = true;
    try {
      await syncConnectionsFromBackend(chatManager, "Background sync");
    } catch (err) {
      // Only log when the bot has no adapters (self-healing scenario)
      if (chatManager.adapterCount() === 0) {
        console.warn(`Background sync: failed (${safeErrorMessage(err)}), will retry in ${SYNC_INTERVAL_MS / 1000}s`);
      }
    } finally {
      backgroundSyncRunning = false;
    }
  }, SYNC_INTERVAL_MS);

  // Don't let the timer prevent process exit
  backgroundSyncTimer.unref();
  console.log(`Background sync: enabled (every ${SYNC_INTERVAL_MS / 1000}s)`);
}

// ── Lazy sync (triggered on demand) ─────────────────────────────────────────

let lazySyncPromise: Promise<boolean> | null = null;

/**
 * Attempts a one-shot sync if the bot currently has no adapters.
 * Returns true if the bot has adapters after the attempt.
 * Deduplicates concurrent calls.
 */
export async function lazySyncIfNeeded(chatManager: ChatManager): Promise<boolean> {
  if (chatManager.getCurrentBot() && chatManager.adapterCount() > 0) {
    return true;
  }

  // Deduplicate concurrent lazy sync calls
  if (lazySyncPromise) return lazySyncPromise;

  lazySyncPromise = (async () => {
    try {
      const healthy = await isBackendHealthy();
      if (!healthy) return false;

      await syncConnectionsFromBackend(chatManager, "Lazy sync");
      return chatManager.adapterCount() > 0;
    } catch (err) {
      console.warn(`Lazy sync: failed (${safeErrorMessage(err)})`);
      return false;
    } finally {
      lazySyncPromise = null;
    }
  })();

  return lazySyncPromise;
}

// ── HTTP server for webhooks ────────────────────────────────────────────────

function startServer(chatManager: ChatManager, discordGateway?: DiscordGatewaySupervisor): void {
  const handleBridge = registerBridgeRoutes(chatManager, () => lazySyncIfNeeded(chatManager));
  const webhookBuffer = new WebhookBuffer(chatManager);

  /**
   * Routes a webhook request to the right per-platform / per-connection handler.
   *
   * Used by both:
   *   1. The HTTP server callback for live requests
   *   2. WebhookBuffer.drain() for replayed requests after a chat-manager rebuild
   *
   * For replayed requests: Node's IncomingMessage stays paused after enqueue
   * (no `data` listener was attached), so internally-buffered chunks remain
   * available when readBody() attaches its listeners during drain. Slow / large
   * payloads where data is still arriving when drain fires continue to stream
   * through normally. Client disconnects during the buffer window are caught
   * by the req.destroyed guard at the top.
   */
  async function handleWebhookRequest(req: IncomingMessage, res: ServerResponse): Promise<void> {
    if (req.destroyed) {
      // Client disconnected during the buffer window — short-circuit to avoid
      // attaching listeners to a dead stream. drain() will resolve the queue
      // entry's promise via .finally().
      try {
        res.writeHead(204);
        res.end();
      } catch {
        // res may also be destroyed (shared socket); ignore
      }
      return;
    }

    // Per-connection webhook endpoint (preferred for multi-workspace)
    const connWebhookMatch = req.method === "POST" && req.url?.match(/^\/api\/webhooks\/([^/]+)$/);
    if (connWebhookMatch) {
      await handleConnectionWebhook(req, res, chatManager, PORT, connWebhookMatch[1]);
      return;
    }

    // Legacy platform webhooks (try all adapters for that platform)
    if (req.method === "POST" && req.url === "/api/slack") {
      await handlePlatformWebhook(req, res, chatManager, PORT, "slack");
      return;
    }
    if (req.method === "POST" && req.url === "/api/discord") {
      await handlePlatformWebhook(req, res, chatManager, PORT, "discord");
      return;
    }
    if (req.method === "POST" && req.url === "/api/teams") {
      await handlePlatformWebhook(req, res, chatManager, PORT, "teams");
      return;
    }
    if (req.method === "POST" && req.url === "/api/telegram") {
      await handlePlatformWebhook(req, res, chatManager, PORT, "telegram");
      return;
    }

    res.writeHead(404);
    res.end("Not Found");
  }

  // Drain buffered webhooks after each rebuild completes (#30).
  // drain() returns void; per-entry handler errors are caught internally
  // by WebhookBuffer.drain() and reported via the entry's .finally() block.
  chatManager.onRebuildComplete(() => {
    webhookBuffer.drain(handleWebhookRequest);
  });

  const server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
    // Health check
    if (req.method === "GET" && req.url === "/health") {
      // RES-286 — return 503 while the chat manager is rebuilding adapters
      // (recycle or sync) so docker healthcheck retries kick the bot only
      // once it's actually wedged, not during legitimate 1-s recycle windows.
      // The full `memory` block lets operators graph RSS between recycles
      // and catch leak regressions early.
      //
      // SECURITY: this endpoint is unauthenticated and exposes
      // `process.memoryUsage()` + uptime. The bot listens on `127.0.0.1:3001`
      // (internal management surface; see docker-compose.yml `bot.ports`).
      // If the bot port is ever exposed beyond loopback, gate this response
      // behind the same `BRIDGE_API_KEY` Bearer auth that `/bridge/*` uses,
      // or move memory/uptime to a separate `/debug/health` route.
      const transitioning = chatManager.isTransitioning();
      jsonResponse(res, transitioning ? 503 : 200, {
        status: transitioning ? "transitioning" : "ok",
        adapters: chatManager.listAdapters(),
        transitioning,
        uptime_seconds: Math.round(process.uptime()),
        memory: process.memoryUsage(),
      });
      return;
    }

    // Bridge endpoints (Chat SDK data fetching for Python backend)
    if (req.url?.startsWith("/bridge/")) {
      await handleBridge(req, res);
      return;
    }

    // Buffer webhook requests during Chat instance transitions
    if (webhookBuffer.shouldBuffer()) {
      await webhookBuffer.enqueue(req, res);
      return;
    }

    await handleWebhookRequest(req, res);
  });

  server.listen(PORT, () => {
    console.log(`Bot server listening on port ${PORT}`);
    console.log(`Connection webhook: POST http://localhost:${PORT}/api/webhooks/{connectionId}`);
    console.log(`Legacy Slack:       POST http://localhost:${PORT}/api/slack`);
    console.log(`Legacy Discord:     POST http://localhost:${PORT}/api/discord`);
    console.log(`Legacy Teams:       POST http://localhost:${PORT}/api/teams`);
    console.log(`Legacy Telegram:    POST http://localhost:${PORT}/api/telegram`);
    console.log(`Bridge API:         GET  http://localhost:${PORT}/bridge/*`);
    console.log(`Health check:       GET  http://localhost:${PORT}/health`);
  });

  // Graceful shutdown
  const shutdown = async () => {
    console.log("Shutting down bot service...");
    if (backgroundSyncTimer) {
      clearInterval(backgroundSyncTimer);
      backgroundSyncTimer = null;
    }
    chatManager.stopAdapterRecycle();
    discordGateway?.stop();
    server.close();
    const bot = chatManager.getCurrentBot();
    if (bot) {
      await bot.shutdown().catch(() => {});
    }
    process.exit(0);
  };

  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

/**
 * Per-connection webhook: routes directly to the adapter by connection ID.
 */
async function handleConnectionWebhook(
  req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  port: number,
  connectionId: string,
): Promise<void> {
  try {
    let bot = chatManager.getCurrentBot();
    if (!bot) {
      // Lazy sync: attempt to recover adapters before returning 503
      const recovered = await lazySyncIfNeeded(chatManager);
      bot = chatManager.getCurrentBot();
      if (!bot || !recovered) {
        jsonResponse(res, 503, { error: "Bot not initialized — adapter sync in progress" });
        return;
      }
    }

    const compositeKey = chatManager.getCompositeKeyForConnection(connectionId);
    if (!compositeKey) {
      jsonResponse(res, 404, { error: `Connection ${connectionId} not found` });
      return;
    }

    let body: string;
    try {
      body = await readBody(req);
    } catch (err) {
      if (err instanceof BodyTooLargeError) {
        console.warn(`Webhook: rejected oversize body (connection ${connectionId}) from ${req.socket?.remoteAddress ?? "unknown"}`);
        res.writeHead(500);
        res.end("Internal Server Error");
        req.destroy();   // preserve prior local-readBody behavior — terminate attacker connection immediately
        return;
      }
      throw err;
    }
    const webReq = new Request(`http://localhost:${port}${req.url}`, {
      method: "POST",
      headers: Object.fromEntries(
        Object.entries(req.headers)
          .filter((e): e is [string, string] => typeof e[1] === "string"),
      ),
      body,
    });

    const webhooks = bot.webhooks as any;
    if (typeof webhooks[compositeKey] === "function") {
      const webRes = await webhooks[compositeKey](webReq);
      console.log(`Webhook handled by connection ${connectionId} (${compositeKey})`);
      if (webRes.status < 400) {
        const platform = compositeKey.split(":", 1)[0];
        if (platform === "telegram") {
          recordTelegramChatFromUpdate(body, connectionId);
        } else if (platform === "teams") {
          recordTeamsConversationFromActivity(body, connectionId);
        }
      }
      res.writeHead(webRes.status, Object.fromEntries(webRes.headers.entries()));
      const resBody = await webRes.text();
      res.end(resBody);
    } else {
      jsonResponse(res, 404, { error: `No webhook handler for connection ${connectionId}` });
    }
  } catch (err) {
    // CodeQL js/tainted-format-string (alert #21): pass the format string
    // as a static literal and the user-tainted value as an argument so it
    // cannot influence format-specifier interpretation downstream.
    console.error("Connection webhook error (%s):", connectionId, safeErrorMessage(err));
    res.writeHead(500);
    res.end("Internal Server Error");
  }
}

/**
 * Legacy platform webhook: tries all adapters for the platform sequentially.
 * The first adapter whose handleWebhook() returns a non-error response wins.
 */
async function handlePlatformWebhook(
  req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  port: number,
  platform: string,
): Promise<void> {
  try {
    let bot = chatManager.getCurrentBot();
    if (!bot) {
      // Lazy sync: attempt to recover adapters before returning 503
      const recovered = await lazySyncIfNeeded(chatManager);
      bot = chatManager.getCurrentBot();
      if (!bot || !recovered) {
        jsonResponse(res, 503, { error: "Bot not initialized — adapter sync in progress" });
        return;
      }
    }

    const adapters = chatManager.getAdaptersByPlatform(platform);
    if (adapters.length === 0) {
      jsonResponse(res, 404, { error: `${platform} adapter not connected` });
      return;
    }

    let body: string;
    try {
      body = await readBody(req);
    } catch (err) {
      if (err instanceof BodyTooLargeError) {
        console.warn(`Webhook: rejected oversize body (platform ${platform}) from ${req.socket?.remoteAddress ?? "unknown"}`);
        res.writeHead(500);
        res.end("Internal Server Error");
        req.destroy();   // preserve prior local-readBody behavior — terminate attacker connection immediately
        return;
      }
      throw err;
    }
    const webhooks = bot.webhooks as any;

    // Try each adapter for the platform; first successful response wins
    for (const { compositeKey, connectionId } of adapters) {
      if (typeof webhooks[compositeKey] !== "function") continue;

      try {
        const webReq = new Request(`http://localhost:${port}${req.url}`, {
          method: "POST",
          headers: Object.fromEntries(
            Object.entries(req.headers)
              .filter((e): e is [string, string] => typeof e[1] === "string"),
          ),
          body,
        });
        const webRes = await webhooks[compositeKey](webReq);

        // If verification succeeded (non-4xx), use this response
        if (webRes.status < 400) {
          console.log(`Legacy ${platform} webhook handled by connection ${connectionId}`);
          if (platform === "telegram") {
            recordTelegramChatFromUpdate(body, connectionId);
          } else if (platform === "teams") {
            recordTeamsConversationFromActivity(body, connectionId);
          }
          res.writeHead(webRes.status, Object.fromEntries(webRes.headers.entries()));
          const resBody = await webRes.text();
          res.end(resBody);
          return;
        }
      } catch {
        // This adapter couldn't handle it, try next
      }
    }

    // No adapter could handle it — try the last one anyway to return its error
    const lastKey = adapters[adapters.length - 1].compositeKey;
    const webReq = new Request(`http://localhost:${port}${req.url}`, {
      method: "POST",
      headers: Object.fromEntries(
        Object.entries(req.headers)
          .filter((e): e is [string, string] => typeof e[1] === "string"),
      ),
      body,
    });
    const webRes = await webhooks[lastKey](webReq);
    res.writeHead(webRes.status, Object.fromEntries(webRes.headers.entries()));
    const resBody = await webRes.text();
    res.end(resBody);
  } catch (err) {
    console.error(`${platform} webhook error:`, safeErrorMessage(err));
    res.writeHead(500);
    res.end("Internal Server Error");
  }
}

/**
 * Parse a Telegram webhook body and register any chat ids it exposes into the
 * bridge's in-memory registry, so `listChannels` can surface groups the bot has
 * been invited to. Telegram has no channel-discovery API per chat-sdk docs
 * ("no native way to discover channels or groups the bot inhabits"), so this is
 * the only way a group becomes visible in the UI's channel list.
 */
function recordTelegramChatFromUpdate(body: string, connectionId: string): void {
  try {
    const update = JSON.parse(body);
    const candidates = [
      update?.message,
      update?.edited_message,
      update?.channel_post,
      update?.edited_channel_post,
      update?.my_chat_member,
      update?.chat_member,
      update?.message_reaction,
      update?.callback_query?.message,
    ];
    for (const evt of candidates) {
      if (evt?.chat) recordTelegramChat(connectionId, evt.chat);
    }
  } catch {
    // malformed body — ignore; the SDK's handler will surface its own error
  }
}

/**
 * Parse a Teams webhook (Bot Framework Activity) body and register the
 * conversation into the bridge's in-memory registry, so `listChannels` can
 * surface channels/chats/DMs the bot has been @mentioned in. Teams/Azure Bot
 * Service has no "list my conversations" API without Microsoft Graph app
 * permissions, so this webhook-driven path is what populates the sidebar until
 * a full Graph implementation lands.
 */
function recordTeamsConversationFromActivity(body: string, connectionId: string): void {
  try {
    const activity = JSON.parse(body);
    if (activity?.conversation?.id) {
      recordTeamsConversation(connectionId, activity);
    }
  } catch {
    // malformed body — ignore; the SDK's handler will surface its own error
  }
}

// readBody, MAX_BODY_SIZE, and BodyTooLargeError are imported from ./http-utils.js.

// ── Main ────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  validateEnv();
  console.log("Initializing Beever Atlas bot...");
  console.log(`Backend URL: ${BACKEND_URL}`);
  console.log(`Redis URL: ${REDIS_URL}`);

  const chatManager = new ChatManager(REDIS_URL, registerHandlers);

  // Attempt to load connections from backend with retry + .env fallback
  await loadConnectionsFromBackend(chatManager);

  // Teams is webhook-only and may not be wired through the backend connection
  // flow yet. Supplement from .env only when the backend didn't provide one —
  // otherwise we'd register a duplicate adapter against the same Azure Bot and
  // every @mention would trigger two replies.
  if (chatManager.getAdaptersByPlatform("teams").length === 0) {
    await registerTeamsFromEnvIfPresent(chatManager, "Env supplement");
  }

  // Start periodic background sync for self-healing
  startBackgroundSync(chatManager);

  // RES-286 — schedule periodic adapter recycle to drop accumulated state in
  // long-lived adapter websockets (notably chat-adapter-mattermost 1.1.2,
  // which leaks ~37 MB/h via its ws message handler closures). Default is
  // 6 h; set ADAPTER_RECYCLE_INTERVAL_MS=0 to disable for local dev.
  //
  // A floor of 60 s applies to any positive override — a too-small interval
  // would thrash the websocket and degrade availability. The `=== 0` escape
  // hatch is preserved so dev/tests can opt out entirely.
  const RECYCLE_DEFAULT_MS = 21_600_000;
  const RECYCLE_FLOOR_MS = 60_000;
  const recycleRaw = parseInt(process.env.ADAPTER_RECYCLE_INTERVAL_MS || `${RECYCLE_DEFAULT_MS}`, 10);
  const recycleMs = !Number.isFinite(recycleRaw)
    ? RECYCLE_DEFAULT_MS
    : recycleRaw === 0
      ? 0
      : Math.max(recycleRaw, RECYCLE_FLOOR_MS);
  chatManager.scheduleAdapterRecycle(recycleMs);

  // Pre-warm the MSAL Graph token for every Teams adapter. The token is shared
  // across all Graph reads (channel enumeration AND message fetches), so this
  // removes the ~1.5–2.5s cold-acquire penalty from the first user request.
  const warmTeamsAdapters = () => {
    for (const { adapter } of chatManager.getAdaptersByPlatform("teams")) {
      warmTeamsGraphToken(adapter);
    }
  };
  warmTeamsAdapters();
  // Re-warm after every adapter rebuild (connection change OR the periodic
  // recycle): a rebuild creates fresh adapter instances whose token cache is
  // empty, which is exactly when the next fetch would otherwise pay the cold
  // penalty. Fire-and-forget and Teams-filtered — a no-op when no Teams
  // adapters are registered.
  chatManager.onRebuildComplete(warmTeamsAdapters);

  // Discord delivers plain channel messages — including ordinary @mentions —
  // only over a Gateway WebSocket that the host must keep alive (Slack uses
  // webhooks, Mattermost its own ws). Run a Discord-scoped keep-alive
  // supervisor: it forwards Gateway events to the bot's own per-connection
  // webhook and is a no-op when no Discord connection is registered, so other
  // platforms are unaffected. Re-sync after every rebuild/recycle because those
  // replace adapter instances the in-flight listeners are bound to.
  const discordGateway = new DiscordGatewaySupervisor(chatManager);
  discordGateway.sync();
  chatManager.onRebuildComplete(() => discordGateway.sync());

  startServer(chatManager, discordGateway);
  console.log("Bot service ready");
}

// Only bootstrap the server when this module is run as the process entrypoint.
// Importing it (e.g. from a unit test that exercises answerInThread/postNotice)
// must NOT start the HTTP server or connect adapters.
const invokedDirectly =
  !!process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  main().catch((err: unknown) => {
    console.error("Fatal error:", safeErrorMessage(err));
    process.exit(1);
  });
}
