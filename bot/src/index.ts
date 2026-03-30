import { config } from "dotenv";
import { resolve } from "node:path";
import { createServer, IncomingMessage, ServerResponse } from "node:http";

// Load .env from project root (one level up from bot/)
config({ path: resolve(import.meta.dirname, "../../.env") });
import { Chat } from "chat";
import { createSlackAdapter } from "@chat-adapter/slack";
import { createRedisState } from "@chat-adapter/state-redis";
import { formatBlockKit } from "./formatter.js";
import { consumeSSEStream } from "./sse-client.js";
import { registerBridgeRoutes } from "./bridge.js";

// ── Environment validation ──────────────────────────────────────────────────

const REQUIRED_ENV = ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"] as const;
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const REDIS_URL = process.env.REDIS_URL || "redis://localhost:6379";
const PORT = parseInt(process.env.BOT_PORT || "3001", 10);

function validateEnv(): void {
  const missing = REQUIRED_ENV.filter((key) => !process.env[key]);
  if (missing.length > 0) {
    console.error(`Missing required environment variables: ${missing.join(", ")}`);
    process.exit(1);
  }
}

// ── Bot setup ───────────────────────────────────────────────────────────────

// Store adapter reference for bridge API access (channel listing, etc.)
// The adapter auto-detects SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET from environment variables.
const slackAdapter = createSlackAdapter();

function createBot(): Chat {
  const bot = new Chat({
    userName: "beever",
    adapters: {
      slack: slackAdapter,
      // Future adapters:
      // teams: createTeamsAdapter(),
      // discord: createDiscordAdapter(),
    },
    state: createRedisState({ url: REDIS_URL }),
  });

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

  return bot;
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

async function askBackend(channelId: string, question: string): Promise<AskResult> {
  const url = `${BACKEND_URL}/api/channels/${channelId}/ask`;
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });

  if (!response.ok) {
    throw new Error(`Backend returned ${response.status}: ${await response.text()}`);
  }

  return consumeSSEStream(response);
}

// ── HTTP server for webhooks ────────────────────────────────────────────────

function startServer(bot: Chat): void {
  // Register bridge routes for Python backend data fetching
  const handleBridge = registerBridgeRoutes(bot, slackAdapter);

  const server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
    // Health check
    if (req.method === "GET" && req.url === "/health") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: "ok" }));
      return;
    }

    // Bridge endpoints (Chat SDK data fetching for Python backend)
    if (req.url?.startsWith("/bridge/")) {
      await handleBridge(req, res);
      return;
    }

    // Slack webhook
    if (req.method === "POST" && req.url === "/api/slack") {
      try {
        const body = await readBody(req);
        const webReq = new Request(`http://localhost:${PORT}${req.url}`, {
          method: "POST",
          headers: Object.fromEntries(
            Object.entries(req.headers)
              .filter((e): e is [string, string] => typeof e[1] === "string")
          ),
          body,
        });

        const webRes = await bot.webhooks.slack(webReq);
        res.writeHead(webRes.status, Object.fromEntries(webRes.headers.entries()));
        const resBody = await webRes.text();
        res.end(resBody);
      } catch (err) {
        console.error("Webhook error:", err);
        res.writeHead(500);
        res.end("Internal Server Error");
      }
      return;
    }

    res.writeHead(404);
    res.end("Not Found");
  });

  server.listen(PORT, () => {
    console.log(`Bot server listening on port ${PORT}`);
    console.log(`Slack webhook: POST http://localhost:${PORT}/api/slack`);
    console.log(`Bridge API:    GET  http://localhost:${PORT}/bridge/*`);
    console.log(`Health check:  GET  http://localhost:${PORT}/health`);
  });

  // Graceful shutdown
  const shutdown = async () => {
    console.log("Shutting down bot service...");
    server.close();
    process.exit(0);
  };

  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk: Buffer) => { data += chunk.toString(); });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  validateEnv();
  console.log("Initializing Beever Atlas bot...");
  console.log(`Backend URL: ${BACKEND_URL}`);
  console.log(`Redis URL: ${REDIS_URL}`);

  const bot = createBot();
  startServer(bot);
  console.log("Bot service ready");
}

main().catch((err: unknown) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
