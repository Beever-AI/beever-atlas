import { config } from "dotenv";
import { resolve } from "node:path";
import { createServer, IncomingMessage, ServerResponse } from "node:http";

// Load .env from project root (one level up from bot/)
config({ path: resolve(import.meta.dirname, "../../.env") });
import { Chat } from "chat";
import { formatBlockKit } from "./formatter.js";
import { consumeSSEStream } from "./sse-client.js";
import { registerBridgeRoutes, recordTelegramChat, recordTeamsConversation } from "./bridge.js";
import { jsonResponse, readBody, MAX_BODY_SIZE, BodyTooLargeError } from "./http-utils.js";
import { ChatManager } from "./chat-manager.js";
import { WebhookBuffer } from "./webhook-buffer.js";

// ── Environment validation ──────────────────────────────────────────────────

const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const REDIS_URL = process.env.REDIS_URL || "redis://localhost:6379";
const PORT = parseInt(process.env.BOT_PORT || "3001", 10);

function validateEnv(): void {
  // No platform-specific env vars are required — connections are loaded at
  // runtime from the backend database or fall back to .env credentials.
}

// ── Handler registration ─────────────────────────────────────────────────────

function registerHandlers(bot: Chat): void {
  // Handler: user @mentions the bot
  bot.onNewMention(async (thread, message) => {
    console.log(`[@mention] ${message.text} (from ${thread.id})`);
    await thread.subscribe();

    const channelId = extractChannelId(thread.id);
    const question = stripMention(message.text || "");

    if (!question.trim()) {
      await thread.post("Please ask me a question! For example: @beever what is our tech stack?");
      return;
    }

    try {
      const result = await askBackend(channelId, question);
      const blocks = formatBlockKit(result.answer, result.citations, result.route);
      await thread.post(blocks);
    } catch (err) {
      console.error("Error processing mention:", err);
      await thread.post("Sorry, I encountered an error processing your question. Please try again.");
    }
  });

  // Handler: follow-up messages in subscribed threads
  bot.onSubscribedMessage(async (thread, message) => {
    console.log(`[subscribed] ${message.text} (in ${thread.id})`);

    const channelId = extractChannelId(thread.id);
    const question = message.text || "";

    if (!question.trim()) return;

    try {
      const result = await askBackend(channelId, question);
      const blocks = formatBlockKit(result.answer, result.citations, result.route);
      await thread.post(blocks);
    } catch (err) {
      console.error("Error processing follow-up:", err);
      await thread.post("Sorry, I encountered an error. Please try again.");
    }
  });
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function extractChannelId(threadId: string): string {
  // Chat SDK thread IDs follow pattern: "slack:CHANNEL_ID:THREAD_TS"
  const parts = threadId.split(":");
  return parts.length >= 2 ? parts[1] : threadId;
}

function stripMention(text: string): string {
  // Remove Slack @mention format: <@U12345> or <@U12345|username>
  return text.replace(/<@[A-Z0-9]+(\|[^>]+)?>/g, "").trim();
}

export interface AskResult {
  answer: string;
  citations: Array<{ type: string; text: string }>;
  route: string;
  confidence: number;
  costUsd: number;
}

function backendApiKey(): string {
  const raw = process.env.BEEVER_API_KEYS || "";
  return raw.split(",").map((k) => k.trim()).find(Boolean) || "";
}

async function askBackend(channelId: string, question: string): Promise<AskResult> {
  const url = `${BACKEND_URL}/api/channels/${channelId}/ask`;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const apiKey = backendApiKey();
  if (apiKey) {
    headers["Authorization"] = `Bearer ${apiKey}`;
  }
  const response = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify({ question }),
  });

  if (!response.ok) {
    throw new Error(`Backend returned ${response.status}: ${await response.text()}`);
  }

  return consumeSSEStream(response);
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
  }

  console.log(`${label}: loaded ${connections.length} connection(s) from backend`);
  return true;
}

async function loadConnectionsFromBackend(chatManager: ChatManager): Promise<void> {
  const delays = [1000, 2000, 4000, 8000, 16000];

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
        console.warn(`Startup sync: attempt ${attempt + 1} failed (${err}), retrying in ${waitMs}ms...`);
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
        console.warn(`Background sync: failed (${err}), will retry in ${SYNC_INTERVAL_MS / 1000}s`);
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
      console.warn(`Lazy sync: failed (${err})`);
      return false;
    } finally {
      lazySyncPromise = null;
    }
  })();

  return lazySyncPromise;
}

// ── HTTP server for webhooks ────────────────────────────────────────────────

function startServer(chatManager: ChatManager): void {
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
      jsonResponse(res, 200, {
        status: "ok",
        adapters: chatManager.listAdapters(),
        transitioning: chatManager.isTransitioning(),
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
    console.error(`Connection webhook error (${connectionId}):`, err);
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
    console.error(`${platform} webhook error:`, err);
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

  startServer(chatManager);
  console.log("Bot service ready");
}

main().catch((err: unknown) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
