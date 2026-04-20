/**
 * Bridge REST API — exposes Chat SDK fetch capabilities to the Python backend.
 *
 * The bot service is the single gateway for all platform communication.
 * These endpoints let the Python backend fetch messages, channels, and threads
 * without needing platform-specific SDKs.
 */

import type { IncomingMessage, ServerResponse } from "node:http";
import { Buffer } from "node:buffer";
import { timingSafeEqual } from "node:crypto";
import { lookup as dnsLookup } from "node:dns/promises";
import { isIP } from "node:net";
import type { SlackAdapter } from "@chat-adapter/slack";
import type { TeamsAdapter } from "@chat-adapter/teams";
import type { Message as ChatSDKMessage } from "chat";
import { cleanSlackMrkdwn } from "./slack-mrkdwn.js";
import type { ChatManager } from "./chat-manager.js";
export type { PlatformErrorShape } from "./bridge/platformError.js";
export { classifyPlatformError } from "./bridge/platformError.js";
import { classifyPlatformError } from "./bridge/platformError.js";

// ── Types ───────────────────────────────────────────────────────────────────

export interface NormalizedMessage {
  content: string;
  author: string;
  author_name: string;
  author_image: string | null;
  platform: string;
  channel_id: string;
  channel_name: string;
  message_id: string;
  timestamp: string;
  thread_id: string | null;
  attachments: Array<{ type: string; url?: string; name?: string }>;
  reactions: Array<{ name: string; count: number }>;
  reply_count: number;
  is_bot: boolean;
  subtype: string | null;
  links: Array<{ url: string; title?: string; description?: string; imageUrl?: string; siteName?: string }>;
}

export interface NormalizedChannel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  topic: string | null;
  purpose: string | null;
}

// ── Platform Bridge interface ────────────────────────────────────────────────

interface GetMessagesOpts {
  limit: number;
  since?: string;
  before?: string;
  order?: string;
}

interface PlatformBridge {
  listChannels(): Promise<NormalizedChannel[]>;
  getChannel(id: string): Promise<NormalizedChannel>;
  getMessages(id: string, opts: GetMessagesOpts): Promise<NormalizedMessage[]>;
  getMessageCount(channelId: string): Promise<number>;
  getThreadMessages(channelId: string, threadId: string): Promise<NormalizedMessage[]>;
  proxyFile(url: string): Promise<{ contentType: string; buffer: Buffer }>;
  resolveUser(userId: string): Promise<{ name: string; image: string | null }>;
}

// ── Auth ────────────────────────────────────────────────────────────────────
//
// Security finding M1 mitigation: BRIDGE_API_KEY is required unconditionally.
// The legacy "no key configured = no auth" dev-mode bypass keyed off
// BEEVER_ENV/NODE_ENV is gone — a staging environment that forgot to set
// BEEVER_ENV=production used to land wide open.
//
// For genuine local development, set BRIDGE_ALLOW_UNAUTH="true" (strict
// string match) AND leave BRIDGE_API_KEY unset. Any other combination
// enforces the bearer.

function constantTimeEqual(a: string, b: string): boolean {
  const aBuf = Buffer.from(a);
  const bBuf = Buffer.from(b);
  if (aBuf.length !== bBuf.length) return false;
  return timingSafeEqual(aBuf, bBuf);
}

function unauthorized(res: ServerResponse): false {
  res.writeHead(401, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Unauthorized", code: "AUTH_FAILED" }));
  return false;
}

export function checkAuth(
  req: IncomingMessage,
  res: ServerResponse,
): boolean {
  // Read env per-call so tests can mutate process.env between requests
  // and operators can swap keys without restarting (cost is negligible —
  // process.env lookup is a hash hit).
  const bridgeKey = process.env.BRIDGE_API_KEY || "";
  const allowUnauth = process.env.BRIDGE_ALLOW_UNAUTH === "true";
  const hmacDual = process.env.BEEVER_BRIDGE_HMAC_DUAL === "true";

  if (!bridgeKey) {
    if (allowUnauth) return true; // explicit local-dev opt-in
    return unauthorized(res);
  }

  const authHeader = req.headers.authorization || "";
  const expected = `Bearer ${bridgeKey}`;
  if (!constantTimeEqual(authHeader, expected)) {
    if (hmacDual && authHeader === expected) {
      console.warn(
        "Bridge auth: accepted via legacy == path (BEEVER_BRIDGE_HMAC_DUAL). Retire flag next release.",
      );
      return true;
    }
    return unauthorized(res);
  }
  return true;
}

/**
 * Fail-fast guard for production misconfiguration. Called from
 * `registerBridgeRoutes` the first time the bridge is wired up, so
 * importing this module (e.g. from a test) is a pure side-effect-free
 * operation.
 */
export function assertBridgeAuthReady(): void {
  const bridgeKey = process.env.BRIDGE_API_KEY || "";
  const allowUnauth = process.env.BRIDGE_ALLOW_UNAUTH === "true";
  const isProd =
    process.env.BEEVER_ENV === "production" ||
    process.env.NODE_ENV === "production";

  if (!bridgeKey && isProd) {
    console.error(
      "FATAL: BRIDGE_API_KEY is required in production (BEEVER_ENV/NODE_ENV=production)",
    );
    process.exit(1);
  }

  if (!bridgeKey && allowUnauth) {
    console.warn(
      "⚠️  BRIDGE_ALLOW_UNAUTH=true — running without bridge authentication. Do NOT use in production.",
    );
  }
}

// ── SSRF guard for proxyFile: block RFC1918, loopback, link-local, cloud metadata ──

const PRIVATE_V4_RANGES: Array<[number, number]> = [
  [0x0a000000, 0xff000000], // 10/8
  [0xac100000, 0xfff00000], // 172.16/12
  [0xc0a80000, 0xffff0000], // 192.168/16
  [0x7f000000, 0xff000000], // 127/8
  [0xa9fe0000, 0xffff0000], // 169.254/16 (incl. 169.254.169.254)
  [0x64400000, 0xffc00000], // 100.64/10 CGNAT
  [0x00000000, 0xff000000], // 0/8
];

function ipv4ToInt(ip: string): number {
  const parts = ip.split(".").map((p) => Number(p));
  if (parts.length !== 4 || parts.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) {
    return -1;
  }
  return ((parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]) >>> 0;
}

function isPrivateIP(ip: string): boolean {
  const family = isIP(ip);
  if (family === 4) {
    const n = ipv4ToInt(ip);
    if (n < 0) return true;
    for (const [base, mask] of PRIVATE_V4_RANGES) {
      if ((n & mask) >>> 0 === (base & mask) >>> 0) return true;
    }
    return false;
  }
  if (family === 6) {
    const lc = ip.toLowerCase();
    if (lc === "::1" || lc === "::") return true;
    if (lc.startsWith("fe80") || lc.startsWith("fc") || lc.startsWith("fd")) return true;
    if (lc.includes(".")) {
      const tail = lc.slice(lc.lastIndexOf(":") + 1);
      if (isIP(tail) === 4 && isPrivateIP(tail)) return true;
    }
    return false;
  }
  return true;
}

async function assertPublicUrl(rawUrl: string): Promise<void> {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error("invalid URL");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(`unsupported scheme: ${parsed.protocol}`);
  }
  const host = parsed.hostname;
  if (!host) throw new Error("missing host");
  if (isIP(host)) {
    if (isPrivateIP(host)) throw new Error(`blocked private IP literal: ${host}`);
    return;
  }
  const { address } = await dnsLookup(host);
  if (isPrivateIP(address)) {
    throw new Error(`host ${host} resolved to private IP ${address}`);
  }
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function jsonResponse(res: ServerResponse, status: number, data: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(data));
}

function parseQuery(url: string): URLSearchParams {
  const idx = url.indexOf("?");
  return new URLSearchParams(idx >= 0 ? url.slice(idx + 1) : "");
}

// classifyPlatformError is imported from ./bridge/platformError.js above and re-exported.

async function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk: Buffer) => { data += chunk.toString(); });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

// ── User profile cache (module-level, persists across requests) ─────────────

const userProfileCache = new Map<string, { name: string; image: string | null }>();
const USER_LOOKUP_CONCURRENCY = 8;

// ── SlackBridge ──────────────────────────────────────────────────────────────

class SlackBridge implements PlatformBridge {
  private adapter: SlackAdapter;

  constructor(adapter: SlackAdapter) {
    this.adapter = adapter;
  }

  async resolveUser(userId: string): Promise<{ name: string; image: string | null }> {
    if (userProfileCache.has(userId)) return userProfileCache.get(userId)!;
    try {
      const result = await (this.adapter as any).client.users.info({ user: userId });
      const profile = result.user?.profile;
      const resolved = {
        name: result.user?.real_name || result.user?.name || userId,
        image: profile?.image_48 || profile?.image_72 || null,
      };
      userProfileCache.set(userId, resolved);
      return resolved;
    } catch {
      const fallback = { name: userId, image: null };
      userProfileCache.set(userId, fallback);
      return fallback;
    }
  }

  async listChannels(): Promise<NormalizedChannel[]> {
    const channels: NormalizedChannel[] = [];
    let cursor: string | undefined;

    do {
      const result = await (this.adapter as any).client.conversations.list({
        types: "public_channel,private_channel",
        limit: 200,
        exclude_archived: true,
        ...(cursor ? { cursor } : {}),
      });

      for (const ch of result.channels || []) {
        channels.push({
          channel_id: ch.id,
          name: ch.name || "",
          platform: "slack",
          is_member: ch.is_member ?? false,
          member_count: ch.num_members ?? null,
          topic: ch.topic?.value ?? null,
          purpose: ch.purpose?.value ?? null,
        });
      }

      cursor = result.response_metadata?.next_cursor;
    } while (cursor);

    return channels;
  }

  async getChannel(id: string): Promise<NormalizedChannel> {
    const result = await (this.adapter as any).client.conversations.info({ channel: id });
    const ch = result.channel;
    return {
      channel_id: id,
      name: ch?.name || "",
      platform: "slack",
      is_member: ch?.is_member ?? false,
      member_count: ch?.num_members ?? null,
      topic: ch?.topic?.value ?? null,
      purpose: ch?.purpose?.value ?? null,
    };
  }

  async getMessages(channelId: string, opts: GetMessagesOpts): Promise<NormalizedMessage[]> {
    const historyParams: Record<string, unknown> = {
      channel: channelId,
      limit: opts.limit,
    };
    if (opts.since) {
      const sinceEpoch = new Date(opts.since).getTime() / 1000;
      historyParams.oldest = String(sinceEpoch);
    }
    if (opts.before) {
      historyParams.latest = opts.before;
    }

    const result = await (this.adapter as any).client.conversations.history(historyParams);
    const rawMessages = result.messages || [];

    // Get channel name
    let channelName = "";
    try {
      const chInfo = await (this.adapter as any).client.conversations.info({ channel: channelId });
      channelName = chInfo.channel?.name || "";
    } catch { /* ignore */ }

    // Resolve user profiles
    const userIds: string[] = [...new Set<string>(
      rawMessages
        .filter((m: any) => m.user && !m.bot_id)
        .map((m: any) => m.user as string),
    )];
    const userMap = new Map<string, { name: string; image: string | null }>();
    for (let i = 0; i < userIds.length; i += USER_LOOKUP_CONCURRENCY) {
      const chunk = userIds.slice(i, i + USER_LOOKUP_CONCURRENCY);
      const resolved = await Promise.all(
        chunk.map(async (uid: string) => [uid, await this.resolveUser(uid)] as const),
      );
      for (const [uid, profile] of resolved) {
        userMap.set(uid, profile);
      }
    }

    const messages: NormalizedMessage[] = rawMessages.map((msg: any) => {
      const authorId: string = msg.user || msg.bot_id || "unknown";
      const userInfo = userMap.get(authorId);
      const subtype: string | undefined = msg.subtype;
      const detectedBot = !!msg.bot_id || subtype === "bot_message";
      const rawText: string = msg.text || "";
      const threadTs: string | undefined = msg.thread_ts;

      return {
        content: cleanSlackMrkdwn(rawText, userMap),
        author: authorId,
        author_name: msg.username || userInfo?.name || authorId,
        author_image: userInfo?.image || null,
        platform: "slack",
        channel_id: channelId,
        channel_name: channelName ? `#${channelName}` : "",
        message_id: msg.ts || "",
        timestamp: new Date(Number.parseFloat(msg.ts || "0") * 1000).toISOString(),
        thread_id: threadTs && threadTs !== msg.ts ? threadTs : null,
        attachments: [
          ...(msg.files || []).map((f: any) => ({
            type: f.mimetype?.startsWith("image/") ? "image"
                : f.mimetype?.startsWith("video/") ? "video"
                : "file",
            url: f.url_private_download || f.url_private || f.permalink,
            name: f.name || f.title,
            mimetype: f.mimetype || "",
          })),
          ...(msg.attachments || [])
            .filter((a: any) => a.image_url && !a.from_url && !a.original_url)
            .map((a: any) => ({
              type: "image" as const,
              url: a.image_url,
              name: a.title || a.fallback || "Image",
              mimetype: "image/png",
            })),
        ],
        reactions: (msg.reactions || []).map((r: any) => ({
          name: r.name,
          count: r.count,
        })),
        reply_count: msg.reply_count || 0,
        is_bot: detectedBot,
        subtype: subtype || null,
        links: (msg.attachments || [])
          .filter((a: any) => a.from_url || a.original_url)
          .map((a: any) => ({
            url: a.from_url || a.original_url,
            title: a.title,
            description: a.text || a.fallback,
            imageUrl: a.image_url || a.thumb_url,
            siteName: a.service_name,
          })),
      };
    });

    if (opts.order === "asc") {
      messages.reverse();
    }
    return messages;
  }

  async getMessageCount(channelId: string): Promise<number> {
    let count = 0;
    let cursor: string | undefined;
    do {
      const params: Record<string, unknown> = { channel: channelId, limit: 200 };
      if (cursor) params.cursor = cursor;
      const result = await (this.adapter as any).client.conversations.history(params);
      count += (result.messages || []).length;
      cursor = result.response_metadata?.next_cursor || undefined;
    } while (cursor);
    return count;
  }

  async getThreadMessages(channelId: string, threadId: string): Promise<NormalizedMessage[]> {
    const result = await (this.adapter as any).client.conversations.replies({
      channel: channelId,
      ts: threadId,
      limit: 200,
    });

    const rawReplies = result.messages || [];

    const userIds: string[] = [...new Set<string>(
      rawReplies
        .filter((m: any) => m.user && !m.bot_id)
        .map((m: any) => m.user as string),
    )];
    const userMap = new Map<string, { name: string; image: string | null }>();
    for (let i = 0; i < userIds.length; i += USER_LOOKUP_CONCURRENCY) {
      const chunk = userIds.slice(i, i + USER_LOOKUP_CONCURRENCY);
      const resolved = await Promise.all(
        chunk.map(async (uid: string) => [uid, await this.resolveUser(uid)] as const),
      );
      for (const [uid, profile] of resolved) {
        userMap.set(uid, profile);
      }
    }

    return rawReplies.map((msg: any) => {
      const authorId: string = msg.user || msg.bot_id || "unknown";
      const userInfo = userMap.get(authorId);
      const subtype: string | undefined = msg.subtype;
      const detectedBot = !!msg.bot_id || subtype === "bot_message";
      const rawText: string = msg.text || "";

      return {
        content: cleanSlackMrkdwn(rawText, userMap),
        author: authorId,
        author_name: msg.username || userInfo?.name || authorId,
        author_image: userInfo?.image || null,
        platform: "slack",
        channel_id: channelId,
        channel_name: "",
        message_id: msg.ts || "",
        timestamp: new Date(Number.parseFloat(msg.ts || "0") * 1000).toISOString(),
        thread_id: threadId,
        attachments: (msg.files || []).map((f: any) => ({
          type: f.mimetype?.startsWith("image/") ? "image" : "file",
          url: f.url_private,
          name: f.name,
          mimetype: f.mimetype || "",
        })),
        reactions: (msg.reactions || []).map((r: any) => ({
          name: r.name,
          count: r.count,
        })),
        reply_count: 0,
        is_bot: detectedBot,
        subtype: subtype || null,
        links: [],
      };
    });
  }

  // ── Request queue: serializes Slack file API calls to avoid rate-limit bursts ──
  private fileRequestQueue: Promise<void> = Promise.resolve();
  private static readonly FILE_REQUEST_SPACING_MS = 200; // minimum ms between file requests

  async proxyFile(fileUrl: string): Promise<{ contentType: string; buffer: Buffer }> {
    // Serialize file requests to avoid hitting Slack rate limits
    return new Promise((resolve, reject) => {
      this.fileRequestQueue = this.fileRequestQueue
        .then(() => this._proxyFileInner(fileUrl).then(resolve, reject))
        .then(() => new Promise<void>((r) => setTimeout(r, SlackBridge.FILE_REQUEST_SPACING_MS)));
    });
  }

  private async _proxyFileInner(
    fileUrl: string,
    retries = 3,
  ): Promise<{ contentType: string; buffer: Buffer }> {
    const decodedUrl = decodeURIComponent(fileUrl);
    await assertPublicUrl(decodedUrl);
    const token = (this.adapter as any).defaultBotToken || (this.adapter as any).getToken();

    let response = await fetch(decodedUrl, {
      headers: { Authorization: `Bearer ${token}` },
    });

    // Retry on 429 with exponential backoff
    if (response.status === 429 && retries > 0) {
      const retryAfter = parseInt(response.headers.get("retry-after") || "2", 10);
      const waitMs = retryAfter * 1000;
      console.log(`Bridge: Slack rate limited (429), retrying after ${retryAfter}s (${retries} retries left)`);
      await new Promise((r) => setTimeout(r, waitMs));
      return this._proxyFileInner(fileUrl, retries - 1);
    }

    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("text/html") && decodedUrl.includes("files-pri")) {
      console.log("Bridge: fileProxy got HTML, trying files.sharedPublicURL fallback");
      const match = decodedUrl.match(/files-pri\/[^/]+-(F[^/]+)\//);
      if (match) {
        const fileId = match[1];
        try {
          const fileInfo = await (this.adapter as any).client.files.info({ file: fileId });
          const downloadUrl = fileInfo.file?.url_private_download || fileInfo.file?.url_private;
          if (downloadUrl) {
            response = await fetch(downloadUrl, {
              headers: { Authorization: `Bearer ${token}` },
            });
            // Retry on 429 for fallback URL too
            if (response.status === 429 && retries > 0) {
              const retryAfter = parseInt(response.headers.get("retry-after") || "2", 10);
              console.log(`Bridge: Slack rate limited on fallback (429), retrying after ${retryAfter}s`);
              await new Promise((r) => setTimeout(r, retryAfter * 1000));
              return this._proxyFileInner(fileUrl, retries - 1);
            }
          }
        } catch (e) {
          console.log("Bridge: files.info fallback failed:", e);
        }
      }
    }

    if (!response.ok) {
      throw new Error(`Failed to fetch file: ${response.status}`);
    }

    const finalContentType = response.headers.get("content-type") || "application/octet-stream";
    if (finalContentType.includes("text/html")) {
      throw new Error("File proxy returned HTML instead of file content — file may be deleted or inaccessible");
    }
    const buffer = Buffer.from(await response.arrayBuffer());
    return { contentType: finalContentType, buffer };
  }
}

// ── DiscordBridge ─────────────────────────────────────────────────────────────

class DiscordBridge implements PlatformBridge {
  private adapter: unknown;
  private botToken: string;

  // ── Request queue: serializes Discord API calls to avoid bursts ──
  private requestQueue: Promise<void> = Promise.resolve();
  private static readonly REQUEST_SPACING_MS = 100; // minimum ms between requests

  // ── Caches ──
  private channelCache: { data: NormalizedChannel[]; expiresAt: number } | null = null;
  private static readonly CHANNEL_CACHE_TTL_MS = 300_000; // 5 minutes — reduces Discord API rate limit pressure
  private channelFetchInFlight: Promise<NormalizedChannel[]> | null = null; // dedup concurrent calls

  constructor(adapter: unknown) {
    this.adapter = adapter;
    this.botToken = (adapter as any).botToken;
  }

  /**
   * Convenience wrapper for Discord REST API calls.
   * Requests are serialized through a queue with spacing to prevent bursts,
   * and rate-limit 429 responses are retried with the server-provided delay.
   */
  private discordApi(path: string, retries = 3): Promise<any> {
    return new Promise((resolve, reject) => {
      this.requestQueue = this.requestQueue
        .then(() => this.executeDiscordRequest(path, retries))
        .then(resolve, reject);
    });
  }

  private async executeDiscordRequest(path: string, retries: number): Promise<any> {
    const res = await fetch(`https://discord.com/api/v10${path}`, {
      headers: { Authorization: `Bot ${this.botToken}` },
    });

    if (res.status === 429 && retries > 0) {
      const rawRetryAfter = parseFloat(res.headers.get("retry-after") || "2") * 1000;
      // Cap wait at 5s — longer waits block the request pipeline and cause frontend timeouts
      const retryAfter = Math.min(rawRetryAfter, 5000);
      console.warn(`DiscordBridge: rate limited on ${path}, retrying in ${retryAfter}ms (server asked ${rawRetryAfter}ms)`);
      await new Promise((r) => setTimeout(r, retryAfter));
      return this.executeDiscordRequest(path, retries - 1);
    }

    if (!res.ok) {
      throw new Error(`Discord API ${path}: ${res.status} ${res.statusText}`);
    }

    // Space out requests to stay under Discord's rate limits
    await new Promise((r) => setTimeout(r, DiscordBridge.REQUEST_SPACING_MS));

    return res.json();
  }

  async resolveUser(userId: string): Promise<{ name: string; image: string | null }> {
    if (userProfileCache.has(userId)) return userProfileCache.get(userId)!;
    try {
      const user = await this.discordApi(`/users/${userId}`);
      const avatarUrl = user.avatar
        ? `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.png`
        : null;
      const resolved = {
        name: user.global_name || user.username || userId,
        image: avatarUrl,
      };
      userProfileCache.set(userId, resolved);
      return resolved;
    } catch {
      const fallback = { name: userId, image: null };
      userProfileCache.set(userId, fallback);
      return fallback;
    }
  }

  async listChannels(): Promise<NormalizedChannel[]> {
    // Return cached result if still fresh
    if (this.channelCache && Date.now() < this.channelCache.expiresAt) {
      return this.channelCache.data;
    }

    // Deduplicate: if a fetch is already in flight, share the same promise
    if (this.channelFetchInFlight) {
      return this.channelFetchInFlight;
    }

    this.channelFetchInFlight = this._fetchChannelsFromDiscord();
    try {
      return await this.channelFetchInFlight;
    } finally {
      this.channelFetchInFlight = null;
    }
  }

  private async _fetchChannelsFromDiscord(): Promise<NormalizedChannel[]> {
    try {
      const guilds: any[] = await this.discordApi("/users/@me/guilds");
      const channels: NormalizedChannel[] = [];

      for (const guild of guilds) {
        try {
          const guildChannels: any[] = await this.discordApi(`/guilds/${guild.id}/channels`);
          const textTypes = new Set([0, 5, 15]);
          for (const ch of guildChannels) {
            if (textTypes.has(ch.type)) {
              channels.push({
                channel_id: ch.id,
                name: ch.name || "",
                platform: "discord",
                is_member: true,
                member_count: ch.member_count ?? null,
                topic: ch.topic ?? null,
                purpose: null,
              });
            }
          }
        } catch (err) {
          console.warn(`DiscordBridge: failed to list channels for guild ${guild.id}:`, err);
        }
      }

      this.channelCache = {
        data: channels,
        expiresAt: Date.now() + DiscordBridge.CHANNEL_CACHE_TTL_MS,
      };
      return channels;
    } catch (err) {
      // If we have stale cached data, return it instead of empty
      if (this.channelCache) {
        console.warn("DiscordBridge: listChannels failed, returning stale cache:", String(err).slice(0, 100));
        return this.channelCache.data;
      }
      console.error("DiscordBridge: listChannels error (no cache):", err);
      return [];
    }
  }

  async getChannel(id: string): Promise<NormalizedChannel> {
    const ch = await this.discordApi(`/channels/${id}`);
    return {
      channel_id: id,
      name: ch.name || "",
      platform: "discord",
      is_member: true,
      member_count: ch.member_count ?? null,
      topic: ch.topic ?? null,
      purpose: null,
    };
  }

  async getMessages(channelId: string, opts: GetMessagesOpts): Promise<NormalizedMessage[]> {
    const limit = Math.min(opts.limit, 100);
    const ch = await this.discordApi(`/channels/${channelId}`);
    let messagesUrl = `/channels/${channelId}/messages?limit=${limit}`;
    if (opts.before) {
      messagesUrl += `&before=${opts.before}`;
    }
    const rawMessages: any[] = await this.discordApi(messagesUrl);
    const messages: NormalizedMessage[] = [];

    for (const m of rawMessages) {
      const avatarUrl = m.author?.avatar
        ? `https://cdn.discordapp.com/avatars/${m.author.id}/${m.author.avatar}.png`
        : null;
      messages.push({
        content: m.content || "",
        author: m.author?.id || "unknown",
        author_name: m.author?.global_name || m.author?.username || "unknown",
        author_image: avatarUrl,
        platform: "discord",
        channel_id: channelId,
        channel_name: ch.name || "",
        message_id: m.id,
        timestamp: m.timestamp || new Date().toISOString(),
        thread_id: m.message_reference?.message_id ?? null,
        attachments: (m.attachments ?? []).map((a: any) => ({
          type: a.content_type?.startsWith("image/") ? "image"
              : a.content_type?.startsWith("video/") ? "video"
              : "file",
          url: a.url,
          name: a.filename,
          mimetype: a.content_type || "",
        })),
        reactions: (m.reactions ?? []).map((r: any) => ({
          name: r.emoji?.name || r.emoji?.id || "?",
          count: r.count ?? 0,
        })),
        reply_count: 0,
        is_bot: m.author?.bot ?? false,
        subtype: null,
        links: (m.embeds ?? [])
          .filter((e: any) => e.url || e.type === "link" || e.type === "article" || e.type === "video")
          .map((e: any) => ({
            url: e.url || "",
            title: e.title,
            description: e.description,
            imageUrl: e.thumbnail?.url || e.image?.url,
            siteName: e.provider?.name,
          })),
      });
    }

    if (opts.order === "asc") {
      messages.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
    } else {
      messages.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());
    }
    return messages;
  }

  async getMessageCount(channelId: string): Promise<number> {
    // Discord doesn't have a count API — paginate through all messages
    let count = 0;
    let beforeId: string | undefined;
    while (true) {
      let url = `/channels/${channelId}/messages?limit=100`;
      if (beforeId) url += `&before=${beforeId}`;
      const batch: any[] = await this.discordApi(url);
      if (batch.length === 0) break;
      count += batch.length;
      beforeId = batch[batch.length - 1].id;
      if (batch.length < 100) break;
    }
    return count;
  }

  async getThreadMessages(channelId: string, threadId: string): Promise<NormalizedMessage[]> {
    // Discord threads are channels themselves — fetch the thread channel
    return this.getMessages(threadId, { limit: 100 });
  }

  async proxyFile(url: string): Promise<{ contentType: string; buffer: Buffer }> {
    const decodedUrl = decodeURIComponent(url);
    await assertPublicUrl(decodedUrl);

    // Discord CDN signed URLs expire. Try the URL as-is first.
    let response = await fetch(decodedUrl);

    // If expired/404, try to refresh the attachment URL via the API.
    // Extract channel ID and message ID from the CDN URL pattern:
    // https://cdn.discordapp.com/attachments/{channel_id}/{attachment_id}/...
    if (!response.ok && decodedUrl.includes("cdn.discordapp.com/attachments/")) {
      const match = decodedUrl.match(/attachments\/(\d+)\/(\d+)\//);
      if (match) {
        const [, channelId, attachmentId] = match;
        try {
          // Fetch recent messages to find one with this attachment
          const msgs: any[] = await this.discordApi(`/channels/${channelId}/messages?limit=50`);
          for (const msg of msgs) {
            for (const att of msg.attachments ?? []) {
              if (att.id === attachmentId || att.url?.includes(attachmentId)) {
                response = await fetch(att.url);
                if (response.ok) break;
              }
            }
            if (response.ok) break;
          }
        } catch {
          // Fall through to error below
        }
      }
    }

    if (!response.ok) {
      throw new Error(`Failed to fetch Discord file: ${response.status}`);
    }
    const contentType = response.headers.get("content-type") || "application/octet-stream";
    const buffer = Buffer.from(await response.arrayBuffer());
    return { contentType, buffer };
  }
}

// ── TeamsBridge ──────────────────────────────────────────────────────────────
// Pull-model ingestion via the Chat SDK Teams adapter's built-in fetch methods
// (fetchMessages, fetchChannelMessages, fetchChannelInfo, listThreads). The
// adapter wraps Microsoft Graph under the hood and honours the bot app's
// authenticated identity. Underlying Graph calls that read chat/channel
// messages hit Teams Protected APIs — the tenant must have a
// Microsoft.GraphServices metered account configured (pay-per-call) or a
// Teams Data API license for those calls to return data.
//
// listChannels still reads from `teamsConversationRegistry` so conversations
// the bot has observed via webhook surface immediately, even before the first
// message-history fetch.

interface TeamsConversationRecord {
  conversationId: string;
  name: string;
  conversationType: string;
  teamId: string | null;
  teamName: string | null;
  channelName: string | null;
  serviceUrl: string | null;
  tenantId: string | null;
  lastSeenAt: number;
}

const teamsConversationRegistry = new Map<string, Map<string, TeamsConversationRecord>>();

export function recordTeamsConversation(
  connectionId: string,
  activity: {
    conversation?: { id?: string; conversationType?: string; name?: string; tenantId?: string };
    channelData?: {
      team?: { id?: string; name?: string };
      channel?: { id?: string; name?: string };
      tenant?: { id?: string };
    };
    serviceUrl?: string;
    from?: { name?: string };
  },
): void {
  const rawConversationId = activity?.conversation?.id;
  if (!connectionId || !rawConversationId) return;

  // Bot Framework appends `;messageid=<ts>` to team-channel conversation IDs,
  // which breaks Graph endpoints that expect the bare channel ID. Strip it so
  // downstream listChannels / getMessages can reuse this ID directly against
  // /teams/{teamId}/channels/{channelId}.
  const conversationId = rawConversationId.split(";")[0];

  const conversationType = activity.conversation?.conversationType || "unknown";
  const teamName = activity.channelData?.team?.name || null;
  const channelName = activity.channelData?.channel?.name || null;

  let name: string;
  if (conversationType === "channel") {
    name = teamName && channelName
      ? `${teamName} / ${channelName}`
      : channelName || teamName || conversationId;
  } else if (conversationType === "groupChat") {
    name = activity.conversation?.name || "Group chat";
  } else {
    name = activity.from?.name || activity.conversation?.name || "Direct message";
  }

  let bucket = teamsConversationRegistry.get(connectionId);
  if (!bucket) {
    bucket = new Map();
    teamsConversationRegistry.set(connectionId, bucket);
  }
  bucket.set(conversationId, {
    conversationId,
    name,
    conversationType,
    teamId: activity.channelData?.team?.id || null,
    teamName,
    channelName,
    serviceUrl: activity.serviceUrl || null,
    tenantId: activity.conversation?.tenantId || activity.channelData?.tenant?.id || null,
    lastSeenAt: Date.now(),
  });
}

function teamsRecordToChannel(entry: TeamsConversationRecord): NormalizedChannel {
  return {
    channel_id: entry.conversationId,
    name: entry.name,
    platform: "teams",
    is_member: true,
    member_count: null,
    topic: entry.conversationType,
    purpose: entry.teamName,
  };
}

const TEAMS_PAGE_SIZE = 50;
const TEAMS_MESSAGE_COUNT_CAP = 500;

class TeamsBridge implements PlatformBridge {
  private adapter: TeamsAdapter;
  private connectionId: string;

  constructor(adapter: TeamsAdapter, connectionId: string) {
    this.adapter = adapter;
    this.connectionId = connectionId;
  }

  async resolveUser(userId: string): Promise<{ name: string; image: string | null }> {
    return { name: userId, image: null };
  }

  async listChannels(): Promise<NormalizedChannel[]> {
    const bucket = teamsConversationRegistry.get(this.connectionId);
    if (!bucket || bucket.size === 0) return [];
    return Array.from(bucket.values()).map(teamsRecordToChannel);
  }

  async getChannel(id: string): Promise<NormalizedChannel> {
    const entry = teamsConversationRegistry.get(this.connectionId)?.get(id);
    if (entry) return teamsRecordToChannel(entry);

    try {
      const info = await this.adapter.fetchChannelInfo(id);
      return {
        channel_id: info.id,
        name: info.name || info.id,
        platform: "teams",
        is_member: true,
        member_count: info.memberCount ?? null,
        topic: null,
        purpose: null,
      };
    } catch {
      return {
        channel_id: id,
        name: id,
        platform: "teams",
        is_member: false,
        member_count: null,
        topic: null,
        purpose: null,
      };
    }
  }

  async getMessageCount(channelId: string): Promise<number> {
    let count = 0;
    let cursor: string | undefined;
    while (count < TEAMS_MESSAGE_COUNT_CAP) {
      const page = await this.fetchPage(channelId, cursor, TEAMS_PAGE_SIZE);
      count += page.messages.length;
      if (!page.nextCursor || page.messages.length === 0) break;
      cursor = page.nextCursor;
    }
    return count;
  }

  async getMessages(channelId: string, opts: GetMessagesOpts): Promise<NormalizedMessage[]> {
    const fetchLimit = Math.max(1, Math.min(opts.limit || 100, TEAMS_MESSAGE_COUNT_CAP));
    const collected: ChatSDKMessage<unknown>[] = [];
    let cursor: string | undefined;
    while (collected.length < fetchLimit) {
      const remaining = fetchLimit - collected.length;
      const pageSize = Math.min(TEAMS_PAGE_SIZE, remaining);
      const page = await this.fetchPage(channelId, cursor, pageSize);
      collected.push(...page.messages);
      if (!page.nextCursor || page.messages.length === 0) break;
      cursor = page.nextCursor;
    }
    const entry = teamsConversationRegistry.get(this.connectionId)?.get(channelId);
    const channelName = entry?.name || channelId;
    return collected.map((m) => this.normalizeMessage(m, channelId, channelName));
  }

  async getThreadMessages(channelId: string, threadId: string): Promise<NormalizedMessage[]> {
    const entry = teamsConversationRegistry.get(this.connectionId)?.get(channelId);
    if (!entry?.serviceUrl) {
      throw Object.assign(
        new Error("Teams serviceUrl unknown — bot must observe an activity in this conversation first"),
        { data: { error: "not_ready" }, code: "NOT_READY" },
      );
    }
    const encodedThreadId = this.adapter.encodeThreadId({
      conversationId: channelId,
      replyToId: threadId,
      serviceUrl: entry.serviceUrl,
    });

    const collected: ChatSDKMessage<unknown>[] = [];
    let cursor: string | undefined;
    while (collected.length < TEAMS_MESSAGE_COUNT_CAP) {
      const result = await this.adapter.fetchMessages(encodedThreadId, {
        cursor,
        limit: TEAMS_PAGE_SIZE,
      });
      collected.push(...result.messages);
      if (!result.nextCursor || result.messages.length === 0) break;
      cursor = result.nextCursor;
    }
    return collected.map((m) => this.normalizeMessage(m, channelId, entry.name));
  }

  async proxyFile(url: string): Promise<{ contentType: string; buffer: Buffer }> {
    const decodedUrl = decodeURIComponent(url);
    await assertPublicUrl(decodedUrl);
    const response = await fetch(decodedUrl);
    if (!response.ok) {
      throw new Error(`Failed to fetch Teams file: ${response.status}`);
    }
    const contentType = response.headers.get("content-type") || "application/octet-stream";
    const buffer = Buffer.from(await response.arrayBuffer());
    return { contentType, buffer };
  }

  private async fetchPage(
    channelId: string,
    cursor: string | undefined,
    limit: number,
  ): Promise<{ messages: ChatSDKMessage<unknown>[]; nextCursor?: string }> {
    const entry = teamsConversationRegistry.get(this.connectionId)?.get(channelId);
    const isTeamChannel = entry?.conversationType === "channel";

    if (isTeamChannel) {
      return this.adapter.fetchChannelMessages(channelId, { cursor, limit });
    }

    if (!entry?.serviceUrl) {
      throw Object.assign(
        new Error("Teams serviceUrl unknown — bot must observe an activity in this conversation first"),
        { data: { error: "not_ready" }, code: "NOT_READY" },
      );
    }
    const encodedThreadId = this.adapter.encodeThreadId({
      conversationId: channelId,
      serviceUrl: entry.serviceUrl,
    });
    return this.adapter.fetchMessages(encodedThreadId, { cursor, limit });
  }

  private normalizeMessage(
    m: ChatSDKMessage<unknown>,
    channelId: string,
    channelName: string,
  ): NormalizedMessage {
    const dateSent = m.metadata?.dateSent;
    const timestamp = dateSent instanceof Date
      ? dateSent.toISOString()
      : new Date(dateSent || Date.now()).toISOString();

    return {
      content: m.text || "",
      author: m.author.userId,
      author_name: m.author.fullName || m.author.userName || m.author.userId,
      author_image: null,
      platform: "teams",
      channel_id: channelId,
      channel_name: channelName,
      message_id: m.id,
      timestamp,
      thread_id: null,
      attachments: m.attachments.map((a) => ({
        type: a.type,
        url: a.url,
        name: a.name,
      })),
      reactions: [],
      reply_count: 0,
      is_bot: m.author.isBot === true,
      subtype: null,
      links: (m.links || []).map((l) => ({
        url: l.url,
        title: l.title,
        description: l.description,
        imageUrl: l.imageUrl,
        siteName: l.siteName,
      })),
    };
  }
}

// ── TelegramBridge ────────────────────────────────────────────────────────────
// Telegram bots are event-driven — they receive messages via webhook but cannot
// pull message history or list group chats. These methods return empty stubs.

// Per-connection registry of Telegram chats the bot has observed. Telegram's Bot API
// has no "list my chats" endpoint (confirmed by chat-sdk.dev/adapters/telegram: "no
// native way to discover channels or groups the bot inhabits"), so we populate this
// registry lazily from incoming webhook/polling updates via recordTelegramChat().
// In-memory only — rebuilt as new updates arrive after restart.
interface TelegramChatEntry {
  chatId: string;
  title: string;
  type: "private" | "group" | "supergroup" | "channel" | string;
  lastSeenAt: number;
}
const telegramChatRegistry = new Map<string, Map<string, TelegramChatEntry>>();

export function recordTelegramChat(
  connectionId: string,
  chat: { id: number | string; title?: string; type?: string; first_name?: string; last_name?: string; username?: string },
): void {
  if (!connectionId || chat?.id === undefined || chat?.id === null) return;
  const chatId = String(chat.id);
  const title =
    chat.title ||
    [chat.first_name, chat.last_name].filter(Boolean).join(" ") ||
    chat.username ||
    chatId;
  let bucket = telegramChatRegistry.get(connectionId);
  if (!bucket) {
    bucket = new Map();
    telegramChatRegistry.set(connectionId, bucket);
  }
  bucket.set(chatId, { chatId, title, type: chat.type || "unknown", lastSeenAt: Date.now() });
}

class TelegramBridge implements PlatformBridge {
  private adapter: unknown;
  private connectionId: string;

  constructor(adapter: unknown, connectionId: string) {
    this.adapter = adapter;
    this.connectionId = connectionId;
  }

  async resolveUser(_userId: string): Promise<{ name: string; image: string | null }> {
    return { name: _userId, image: null };
  }

  async listChannels(): Promise<NormalizedChannel[]> {
    const bucket = telegramChatRegistry.get(this.connectionId);
    if (!bucket || bucket.size === 0) return [];
    return Array.from(bucket.values()).map((c) => ({
      channel_id: c.chatId,
      name: c.title,
      platform: "telegram",
      is_member: true,
      member_count: null,
      topic: c.type,
      purpose: null,
    }));
  }

  async getChannel(id: string): Promise<NormalizedChannel> {
    return {
      channel_id: id,
      name: id,
      platform: "telegram",
      is_member: false,
      member_count: null,
      topic: null,
      purpose: null,
    };
  }

  async getMessageCount(_channelId: string): Promise<number> {
    throw Object.assign(
      new Error("Telegram bots cannot count messages"),
      { data: { error: "not_supported" }, code: "NOT_SUPPORTED" },
    );
  }

  async getMessages(_channelId: string, _opts: GetMessagesOpts): Promise<NormalizedMessage[]> {
    throw Object.assign(
      new Error("Telegram bots cannot fetch message history"),
      { data: { error: "not_supported" }, code: "NOT_SUPPORTED" },
    );
  }

  async getThreadMessages(_channelId: string, _threadId: string): Promise<NormalizedMessage[]> {
    throw Object.assign(
      new Error("Telegram bots cannot fetch thread messages"),
      { data: { error: "not_supported" }, code: "NOT_SUPPORTED" },
    );
  }

  async proxyFile(url: string): Promise<{ contentType: string; buffer: Buffer }> {
    const decodedUrl = decodeURIComponent(url);
    await assertPublicUrl(decodedUrl);
    const response = await fetch(decodedUrl);
    if (!response.ok) {
      throw new Error(`Failed to fetch Telegram file: ${response.status}`);
    }
    const contentType = response.headers.get("content-type") || "application/octet-stream";
    const buffer = Buffer.from(await response.arrayBuffer());
    return { contentType, buffer };
  }
}

// ── MattermostBridge ──────────────────────────────────────────────────────────
// Calls Mattermost REST API v4 directly for channel listing and message history.
// The chat-adapter-mattermost community adapter handles real-time WebSocket events
// but does not expose listing/history methods.

const mattermostUserCache = new Map<string, { name: string; image: string | null }>();

class MattermostBridge implements PlatformBridge {
  private baseUrl: string;
  private botToken: string;
  private connectionId: string;
  private botUserId: string | null = null;

  constructor(_adapter: unknown, connectionId: string, baseUrl: string, botToken: string) {
    this.connectionId = connectionId;
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.botToken = botToken;
  }

  private async _fetch(path: string, init?: RequestInit): Promise<any> {
    const url = `${this.baseUrl}/api/v4${path}`;
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.botToken}`,
      "Content-Type": "application/json",
      ...(init?.headers as Record<string, string> || {}),
    };

    let lastErr: Error | null = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      const response = await fetch(url, { ...init, headers });
      if (response.ok) return response.json();

      if (response.status === 429 || response.status >= 500) {
        lastErr = new Error(`Mattermost API ${response.status} on ${path}`);
        const wait = 1000 * (2 ** attempt);
        console.warn(`MattermostBridge: ${response.status} on ${path}, retrying in ${wait}ms (attempt ${attempt + 1}/3)`);
        await new Promise((r) => setTimeout(r, wait));
        continue;
      }

      const text = await response.text().catch(() => "");
      throw new Error(`Mattermost API error ${response.status} on ${path}: ${text}`);
    }
    throw lastErr || new Error(`Mattermost API failed after retries on ${path}`);
  }

  private async _getBotUserId(): Promise<string> {
    if (this.botUserId) return this.botUserId;
    const me = await this._fetch("/users/me");
    this.botUserId = me.id;
    return me.id;
  }

  async resolveUser(userId: string): Promise<{ name: string; image: string | null }> {
    if (mattermostUserCache.has(userId)) return mattermostUserCache.get(userId)!;
    try {
      const user = await this._fetch(`/users/${userId}`);
      const name = [user.first_name, user.last_name].filter(Boolean).join(" ") || user.username || userId;
      const resolved = { name, image: null };
      mattermostUserCache.set(userId, resolved);
      return resolved;
    } catch {
      const fallback = { name: userId, image: null };
      mattermostUserCache.set(userId, fallback);
      return fallback;
    }
  }

  async listChannels(): Promise<NormalizedChannel[]> {
    const botId = await this._getBotUserId();
    const teams: any[] = await this._fetch("/users/me/teams");
    const channels: NormalizedChannel[] = [];

    for (const team of teams) {
      const teamChannels: any[] = await this._fetch(`/users/${botId}/teams/${team.id}/channels`);
      for (const ch of teamChannels) {
        if (ch.type === "D" || ch.type === "G") continue;
        if (ch.delete_at > 0) continue;
        channels.push({
          channel_id: ch.id,
          name: ch.display_name || ch.name || "",
          platform: "mattermost",
          is_member: true,
          member_count: null,
          topic: ch.header || null,
          purpose: ch.purpose || null,
        });
      }
    }
    return channels;
  }

  async getChannel(id: string): Promise<NormalizedChannel> {
    const ch = await this._fetch(`/channels/${id}`);
    return {
      channel_id: ch.id,
      name: ch.display_name || ch.name || "",
      platform: "mattermost",
      is_member: true,
      member_count: null,
      topic: ch.header || null,
      purpose: ch.purpose || null,
    };
  }

  private _normalizePost(post: any, channelName: string, userMap: Map<string, { name: string; image: string | null }>): NormalizedMessage {
    const authorId = post.user_id || "unknown";
    const userInfo = userMap.get(authorId);
    const files: any[] = post.metadata?.files || [];

    return {
      content: post.message || "",
      author: authorId,
      author_name: userInfo?.name || authorId,
      author_image: userInfo?.image || null,
      platform: "mattermost",
      channel_id: post.channel_id || "",
      channel_name: channelName,
      message_id: post.id || "",
      timestamp: new Date(post.create_at).toISOString(),
      thread_id: post.root_id || null,
      attachments: files.map((f: any) => ({
        type: f.mime_type?.startsWith("image/") ? "image"
            : f.mime_type?.startsWith("video/") ? "video"
            : "file",
        url: `${this.baseUrl}/api/v4/files/${f.id}`,
        name: f.name || "",
        mimetype: f.mime_type || "",
      })),
      reactions: [],
      reply_count: post.reply_count || 0,
      is_bot: false,
      subtype: null,
      links: [],
    };
  }

  async getMessages(channelId: string, opts: GetMessagesOpts): Promise<NormalizedMessage[]> {
    // Mattermost API always returns newest-first and doesn't support forward
    // cursor pagination via `since` the way Slack does. For ascending order
    // (used by the sync runner), we walk backward with `before` cursor across
    // multiple internal pages, then reverse to chronological order.
    if (opts.order === "asc" && !opts.before) {
      return this._getMessagesAsc(channelId, opts);
    }

    const params = new URLSearchParams({
      per_page: String(opts.limit || 100),
      // Mattermost default is collapsed threads which omits reply_count/last_reply_at.
      // Without these flags, root posts look replyless and thread ingestion silently skips them.
      collapsedThreads: "true",
      collapsedThreadsExtended: "true",
    });
    if (opts.before) params.set("before", opts.before);
    if (opts.since) {
      const epochMs = new Date(opts.since).getTime();
      params.set("since", String(epochMs));
    }

    const data = await this._fetch(`/channels/${channelId}/posts?${params}`);
    const order: string[] = data.order || [];
    const posts: Record<string, any> = data.posts || {};

    let channelName = "";
    try {
      const ch = await this._fetch(`/channels/${channelId}`);
      channelName = ch.display_name || ch.name || "";
    } catch { /* ignore */ }

    const userIds = [...new Set(order.map((id) => posts[id]?.user_id).filter(Boolean))];
    const userMap = new Map<string, { name: string; image: string | null }>();
    for (const uid of userIds) {
      userMap.set(uid, await this.resolveUser(uid));
    }

    // Mattermost's /posts endpoint returns root posts, thread replies, AND
    // system messages (join/leave/rename) mixed together. We only want
    // user-authored root posts — matching Mattermost's total_msg_count_root.
    // Thread replies are fetched separately via getThreadMessages() when
    // reply_count > 0.
    const messages = order
      .filter((id) => posts[id] && !posts[id].root_id && !posts[id].type)
      .map((id) => this._normalizePost(posts[id], channelName, userMap));

    if (opts.order === "asc") messages.reverse();
    return messages;
  }

  private async _getMessagesAsc(channelId: string, opts: GetMessagesOpts): Promise<NormalizedMessage[]> {
    const totalLimit = opts.limit || 500;
    const pageSize = 200;
    const allPosts: Array<{ id: string; post: any }> = [];
    let beforeCursor: string | undefined;
    const sinceMs = opts.since ? new Date(opts.since).getTime() : 0;

    let channelName = "";
    try {
      const ch = await this._fetch(`/channels/${channelId}`);
      channelName = ch.display_name || ch.name || "";
    } catch { /* ignore */ }

    // Walk backward from newest, collecting pages until we've reached
    // the `since` cutoff or exhausted all messages.
    while (allPosts.length < totalLimit) {
      const params = new URLSearchParams({
        per_page: String(pageSize),
        collapsedThreads: "true",
        collapsedThreadsExtended: "true",
      });
      if (beforeCursor) params.set("before", beforeCursor);

      const data = await this._fetch(`/channels/${channelId}/posts?${params}`);
      const order: string[] = data.order || [];
      const posts: Record<string, any> = data.posts || {};

      if (order.length === 0) break;

      let reachedSince = false;
      for (const id of order) {
        const post = posts[id];
        if (!post) continue;
        // Skip thread replies — they're fetched separately via getThreadMessages().
        if (post.root_id) continue;
        // Skip system messages (join/leave/rename) — not user content.
        if (post.type) continue;
        if (sinceMs > 0 && post.create_at <= sinceMs) {
          reachedSince = true;
          break;
        }
        allPosts.push({ id, post });
      }

      if (reachedSince) break;
      if (order.length < pageSize) break; // last page

      // Cursor: oldest post in this page (last in order array)
      beforeCursor = order[order.length - 1];
    }

    // Reverse to chronological order (oldest first)
    allPosts.reverse();

    // Apply limit
    const limited = allPosts.slice(0, totalLimit);

    // Resolve users
    const userIds = [...new Set(limited.map((e) => e.post.user_id).filter(Boolean))];
    const userMap = new Map<string, { name: string; image: string | null }>();
    for (const uid of userIds) {
      userMap.set(uid, await this.resolveUser(uid));
    }

    return limited.map((e) => this._normalizePost(e.post, channelName, userMap));
  }

  async getMessageCount(channelId: string): Promise<number> {
    const ch = await this._fetch(`/channels/${channelId}`);
    return ch.total_msg_count_root ?? ch.total_msg_count ?? 0;
  }

  async getThreadMessages(channelId: string, threadId: string): Promise<NormalizedMessage[]> {
    const data = await this._fetch(`/posts/${threadId}/thread`);
    const posts: Record<string, any> = data.posts || {};

    let channelName = "";
    try {
      const ch = await this._fetch(`/channels/${channelId}`);
      channelName = ch.display_name || ch.name || "";
    } catch { /* ignore */ }

    const replies = Object.values(posts)
      .filter((p: any) => p.id !== threadId)
      .sort((a: any, b: any) => a.create_at - b.create_at);

    const userIds = [...new Set(replies.map((p: any) => p.user_id).filter(Boolean))];
    const userMap = new Map<string, { name: string; image: string | null }>();
    for (const uid of userIds) {
      userMap.set(uid as string, await this.resolveUser(uid as string));
    }

    return replies.map((p: any) => this._normalizePost(p, channelName, userMap));
  }

  async proxyFile(url: string): Promise<{ contentType: string; buffer: Buffer }> {
    const fileUrl = url.startsWith("http") ? url : `${this.baseUrl}${url}`;
    const response = await fetch(fileUrl, {
      headers: { Authorization: `Bearer ${this.botToken}` },
    });
    if (!response.ok) {
      throw new Error(`Failed to fetch Mattermost file: ${response.status}`);
    }
    const contentType = response.headers.get("content-type") || "application/octet-stream";
    const buffer = Buffer.from(await response.arrayBuffer());
    return { contentType, buffer };
  }
}

// ── Bridge factory (singleton per connection) ────────────────────────────────

/** Persistent bridge instances keyed by "{platform}:{connectionId}".
 *  Cleared when ChatManager rebuilds adapters. */
const bridgeCache = new Map<string, PlatformBridge>();

function clearBridgeCache(): void {
  bridgeCache.clear();
}

function newBridgeForPlatform(platform: string, adapter: unknown, connectionId: string, chatManager?: ChatManager): PlatformBridge | null {
  if (platform === "slack") return new SlackBridge(adapter as SlackAdapter);
  if (platform === "discord") return new DiscordBridge(adapter);
  if (platform === "teams") return new TeamsBridge(adapter as TeamsAdapter, connectionId);
  if (platform === "telegram") return new TelegramBridge(adapter, connectionId);
  if (platform === "mattermost") {
    const config = chatManager?.getAdapterConfig(connectionId);
    const baseUrl = config?.baseUrl || config?.server_url || "";
    const botToken = config?.botToken || config?.bot_token || "";
    if (!baseUrl || !botToken) {
      console.error(`MattermostBridge: missing baseUrl or botToken for connection ${connectionId}`);
      return null;
    }
    return new MattermostBridge(adapter, connectionId, baseUrl, botToken);
  }
  return null;
}

function getOrCreateBridge(platform: string, connectionId: string, adapter: unknown, chatManager?: ChatManager): PlatformBridge | null {
  const key = `${platform}:${connectionId}`;
  const cached = bridgeCache.get(key);
  if (cached) return cached;
  const bridge = newBridgeForPlatform(platform, adapter, connectionId, chatManager);
  if (bridge) bridgeCache.set(key, bridge);
  return bridge;
}

function getBridge(chatManager: ChatManager, platform: string, connectionId?: string): PlatformBridge | null {
  if (connectionId) {
    const entry = chatManager.getAdapterByConnectionId(connectionId);
    if (!entry) return null;
    return getOrCreateBridge(entry.platform, entry.connectionId, entry.adapter, chatManager);
  }
  const adapter = chatManager.getAdapter(platform);
  if (!adapter) return null;
  return getOrCreateBridge(platform, platform, adapter, chatManager);
}

function getBridgeByConnectionId(chatManager: ChatManager, connectionId: string): { platform: string; bridge: PlatformBridge } | null {
  const entry = chatManager.getAdapterByConnectionId(connectionId);
  if (entry) {
    const bridge = getOrCreateBridge(entry.platform, entry.connectionId, entry.adapter, chatManager);
    if (bridge) return { platform: entry.platform, bridge };
  }

  // Fallback for platforms like Mattermost whose bridge uses direct REST calls and
  // doesn't need the Chat SDK adapter instance. The adapter may not be in the bot's
  // internal map (WebSocket init timing) but ChatManager has the credentials.
  const info = chatManager.getConnectionInfo(connectionId);
  if (info) {
    const bridge = getOrCreateBridge(info.platform, connectionId, null, chatManager);
    if (bridge) return { platform: info.platform, bridge };
  }
  return null;
}

function getFirstBridge(chatManager: ChatManager): { platform: string; bridge: PlatformBridge } | null {
  const adapters = chatManager.listAdapters();
  for (const { platform, connectionId } of adapters) {
    const bridge = getBridge(chatManager, platform, connectionId);
    if (bridge) return { platform, bridge };
  }
  return null;
}

/** Infer platform from a file URL pattern. */
function detectPlatformFromUrl(url: string): string | null {
  if (url.includes("files.slack.com") || url.includes("slack-files.com")) return "slack";
  if (url.includes("cdn.discordapp.com") || url.includes("media.discordapp.net")) return "discord";
  if (url.includes("graph.microsoft.com") || url.includes("sharepoint.com")) return "teams";
  if (url.includes("api.telegram.org")) return "telegram";
  if (url.includes("/api/v4/files/")) return "mattermost";
  return null;
}

/**
 * Extract a workspace/team identifier from a file URL for multi-workspace routing.
 * Returns null if no identifier can be extracted.
 *
 * - Slack: files-pri/{TEAM_ID}-{FILE_ID}/ → TEAM_ID (e.g. "T0APJ2FNUKZ")
 * - Telegram: api.telegram.org/file/bot{TOKEN}/... → bot token prefix
 * - Discord/Teams: not reliably extractable from URL
 */
function extractWorkspaceIdFromUrl(url: string): string | null {
  // Slack: extract team ID from files-pri/TEAM_ID-FILE_ID/
  const slackMatch = url.match(/files-pri\/([A-Z0-9]+)-[A-Z0-9]+\//);
  if (slackMatch) return slackMatch[1];

  // Telegram: extract bot token from api.telegram.org/file/bot{TOKEN}/
  const telegramMatch = url.match(/api\.telegram\.org\/file\/bot([^/]+)\//);
  if (telegramMatch) return telegramMatch[1];

  return null;
}

/** Try to detect platform from a channel ID format. */
function detectPlatformFromChannelId(channelId: string): string | null {
  // Slack: starts with C, D, or G followed by alphanumeric (e.g., C0AMY9QSPB2)
  if (/^[CDG][A-Z0-9]{8,}$/.test(channelId)) return "slack";
  // Discord: pure numeric snowflake IDs (e.g., 680671916943605760)
  if (/^\d{17,20}$/.test(channelId)) return "discord";
  return null;
}

// ── Route handlers ──────────────────────────────────────────────────────────

async function handleListChannels(
  req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  platform?: string,
): Promise<void> {
  try {
    if (platform) {
      const bridge = getBridge(chatManager, platform);
      if (!bridge) {
        jsonResponse(res, 404, { error: `Platform "${platform}" not connected`, code: "NOT_FOUND" });
        return;
      }
      const channels = await bridge.listChannels();
      jsonResponse(res, 200, { channels });
    } else {
      // Aggregate from all adapters (use connectionId to avoid duplicates)
      const allChannels: NormalizedChannel[] = [];
      for (const { platform: p, connectionId } of chatManager.listAdapters()) {
        const bridge = getBridge(chatManager, p, connectionId);
        if (bridge) {
          try {
            const channels = await bridge.listChannels();
            allChannels.push(...channels);
          } catch (err) {
            console.error(`Bridge: listChannels error for ${p} (${connectionId}):`, err);
          }
        }
      }
      jsonResponse(res, 200, { channels: allChannels });
    }
  } catch (err) {
    console.error("Bridge: listChannels error:", err);
    const classified = classifyPlatformError(err);
    jsonResponse(res, classified.status, { error: String(err), code: classified.code });
  }
}

async function handleGetChannel(
  _req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  channelId: string,
  platform?: string,
): Promise<void> {
  try {
    const resolvedPlatform = platform || detectPlatformFromChannelId(channelId);
    let bridge: PlatformBridge | null = null;
    if (resolvedPlatform) {
      bridge = getBridge(chatManager, resolvedPlatform);
    }
    if (!bridge) {
      const first = getFirstBridge(chatManager);
      bridge = first?.bridge ?? null;
    }

    if (!bridge) {
      jsonResponse(res, 404, { error: `Channel ${channelId} not found`, code: "NOT_FOUND" });
      return;
    }

    const channel = await bridge.getChannel(channelId);
    jsonResponse(res, 200, channel);
  } catch (err) {
    console.error("Bridge: getChannel error:", err);
    jsonResponse(res, 404, { error: `Channel ${channelId} not found`, code: "NOT_FOUND" });
  }
}

async function handleGetMessages(
  req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  channelId: string,
  platform?: string,
): Promise<void> {
  try {
    const query = parseQuery(req.url || "");
    const limit = Math.min(parseInt(query.get("limit") || "100", 10), 500);
    const since = query.get("since") ?? undefined;
    const before = query.get("before") ?? undefined;
    const order = query.get("order") ?? "desc";

    const resolvedPlatform = platform || detectPlatformFromChannelId(channelId);
    let bridge: PlatformBridge | null = null;
    if (resolvedPlatform) {
      bridge = getBridge(chatManager, resolvedPlatform);
    }
    if (!bridge) {
      const first = getFirstBridge(chatManager);
      bridge = first?.bridge ?? null;
    }

    if (!bridge) {
      jsonResponse(res, 503, { error: "No platform adapters connected", code: "NO_ADAPTER" });
      return;
    }

    const messages = await bridge.getMessages(channelId, { limit, since, before, order });
    jsonResponse(res, 200, { messages });
  } catch (err) {
    console.error("Bridge: getMessages error:", err);
    const classified = classifyPlatformError(err);
    jsonResponse(res, classified.status, { error: String(err), code: classified.code });
  }
}

async function handleGetMessageCount(
  _req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  channelId: string,
  platform?: string,
): Promise<void> {
  try {
    const resolvedPlatform = platform || detectPlatformFromChannelId(channelId);
    let bridge: PlatformBridge | null = null;
    if (resolvedPlatform) {
      bridge = getBridge(chatManager, resolvedPlatform);
    }
    if (!bridge) {
      const first = getFirstBridge(chatManager);
      bridge = first?.bridge ?? null;
    }

    if (!bridge) {
      jsonResponse(res, 503, { error: "No platform adapters connected", code: "NO_ADAPTER" });
      return;
    }

    const count = await bridge.getMessageCount(channelId);
    jsonResponse(res, 200, { count });
  } catch (err) {
    console.error("Bridge: getMessageCount error:", err);
    const classified = classifyPlatformError(err);
    jsonResponse(res, classified.status, { error: String(err), code: classified.code });
  }
}

async function handleGetThreadMessages(
  _req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  channelId: string,
  threadId: string,
  platform?: string,
): Promise<void> {
  try {
    const resolvedPlatform = platform || detectPlatformFromChannelId(channelId);
    let bridge: PlatformBridge | null = null;
    if (resolvedPlatform) {
      bridge = getBridge(chatManager, resolvedPlatform);
    }
    if (!bridge) {
      const first = getFirstBridge(chatManager);
      bridge = first?.bridge ?? null;
    }

    if (!bridge) {
      jsonResponse(res, 503, { error: "No platform adapters connected", code: "NO_ADAPTER" });
      return;
    }

    const messages = await bridge.getThreadMessages(channelId, threadId);
    jsonResponse(res, 200, { messages });
  } catch (err) {
    console.error("Bridge: getThreadMessages error:", err);
    const classified = classifyPlatformError(err);
    jsonResponse(res, classified.status, { error: String(err), code: classified.code });
  }
}

async function handleFileProxy(
  req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  fileUrl: string,
  platform?: string,
  connectionId?: string,
): Promise<void> {
  try {
    const resolvedPlatform = platform || detectPlatformFromUrl(fileUrl);

    // Layer A: Explicit connectionId (highest priority)
    let bridge: PlatformBridge | null = null;
    if (connectionId && resolvedPlatform) {
      bridge = getBridge(chatManager, resolvedPlatform, connectionId);
    }

    // Layer B: Extract workspace ID from URL and match to cached adapter identity
    if (!bridge && resolvedPlatform) {
      const workspaceId = extractWorkspaceIdFromUrl(fileUrl);
      if (workspaceId) {
        const resolvedConnId = chatManager.getConnectionForWorkspaceId(workspaceId);
        if (resolvedConnId) {
          bridge = getBridge(chatManager, resolvedPlatform, resolvedConnId);
        }
      }
    }

    // Layer C: Try all adapters for this platform (fallback for multi-workspace)
    if (!bridge && resolvedPlatform) {
      const allAdapters = chatManager.getAdaptersByPlatform(resolvedPlatform);
      if (allAdapters.length === 1) {
        // Single adapter — use directly (no try-all overhead)
        bridge = getOrCreateBridge(resolvedPlatform, allAdapters[0].connectionId, allAdapters[0].adapter);
      } else if (allAdapters.length > 1) {
        // Multiple adapters — try each until one succeeds
        let lastErr: unknown = null;
        for (const entry of allAdapters) {
          const candidate = getOrCreateBridge(resolvedPlatform, entry.connectionId, entry.adapter);
          if (!candidate) continue;
          try {
            const { contentType, buffer } = await candidate.proxyFile(fileUrl);
            res.writeHead(200, {
              "Content-Type": contentType,
              "Cache-Control": "public, max-age=3600",
            });
            res.end(buffer);
            return; // Success — done
          } catch (err) {
            lastErr = err;
            // Wrong workspace token — try next adapter
          }
        }
        // All adapters failed
        console.error("Bridge: fileProxy all adapters failed:", lastErr);
        const classified = classifyPlatformError(lastErr);
        jsonResponse(res, classified.status, { error: String(lastErr), code: classified.code });
        return;
      }
    }

    // Final fallback: first available bridge of any platform
    if (!bridge) {
      const first = getFirstBridge(chatManager);
      bridge = first?.bridge ?? null;
    }

    if (!bridge) {
      jsonResponse(res, 503, { error: "No platform adapters connected", code: "NO_ADAPTER" });
      return;
    }

    const { contentType, buffer } = await bridge.proxyFile(fileUrl);
    res.writeHead(200, {
      "Content-Type": contentType,
      "Cache-Control": "public, max-age=3600",
    });
    res.end(buffer);
  } catch (err) {
    console.error("Bridge: fileProxy error:", err);
    const classified = classifyPlatformError(err);
    jsonResponse(res, classified.status, { error: String(err), code: classified.code });
  }
}

// ── Adapter management handlers ──────────────────────────────────────────────

async function handleRegisterAdapter(
  req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
): Promise<void> {
  try {
    const body = await readBody(req);
    const { platform, credentials, connectionId } = JSON.parse(body);

    if (!platform || typeof platform !== "string") {
      jsonResponse(res, 400, { error: "Missing required field: platform", code: "INVALID_REQUEST" });
      return;
    }
    if (!credentials || typeof credentials !== "object") {
      jsonResponse(res, 400, { error: "Missing required field: credentials", code: "INVALID_REQUEST" });
      return;
    }

    // Normalize credential keys: frontend/backend sends snake_case, ChatSDK expects camelCase
    const normalizedCreds: Record<string, string> = {};
    for (const [key, value] of Object.entries(credentials)) {
      const camelKey = key.replace(/_([a-z])/g, (_: string, c: string) => c.toUpperCase());
      normalizedCreds[camelKey] = value as string;
    }

    await chatManager.register(platform, normalizedCreds, connectionId || undefined);
    jsonResponse(res, 200, { status: "ok", platform, connectionId: connectionId || platform });
  } catch (err) {
    console.error("Bridge: registerAdapter error:", err);
    jsonResponse(res, 500, { status: "error", message: String(err) });
  }
}

async function handleUnregisterAdapter(
  _req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  connectionIdOrPlatform: string,
): Promise<void> {
  try {
    // Try to unregister by connection ID first, then fall back to platform name
    const found = await chatManager.unregisterByConnectionId(connectionIdOrPlatform);
    if (!found) {
      // Legacy fallback: treat as platform name
      await chatManager.unregister(connectionIdOrPlatform);
    }
    jsonResponse(res, 200, { status: "ok" });
  } catch (err) {
    console.error("Bridge: unregisterAdapter error:", err);
    jsonResponse(res, 500, { status: "error", message: String(err) });
  }
}

async function handleValidateAdapter(
  req: IncomingMessage,
  res: ServerResponse,
  platform: string,
): Promise<void> {
  try {
    const body = await readBody(req);
    const { credentials } = JSON.parse(body);

    if (!credentials || typeof credentials !== "object") {
      jsonResponse(res, 400, { error: "Missing required field: credentials", code: "INVALID_REQUEST" });
      return;
    }

    if (platform === "slack") {
      const { createSlackAdapter } = await import("@chat-adapter/slack");
      const tempAdapter = createSlackAdapter({
        botToken: credentials.botToken,
        signingSecret: credentials.signingSecret,
      });
      // Test API call — auth.test verifies the token
      await (tempAdapter as any).client.auth.test();
      jsonResponse(res, 200, { valid: true });
    } else if (platform === "discord") {
      // Validate token via Discord REST API directly
      const discordRes = await fetch("https://discord.com/api/v10/users/@me", {
        headers: { Authorization: `Bot ${credentials.botToken}` },
      });
      if (!discordRes.ok) {
        jsonResponse(res, 200, { valid: false, error: `Discord API returned ${discordRes.status}` });
      } else {
        jsonResponse(res, 200, { valid: true });
      }
    } else if (platform === "teams") {
      const { createTeamsAdapter } = await import("@chat-adapter/teams" as any);
      const tempAdapter = createTeamsAdapter({
        appId: credentials.appId,
        appPassword: credentials.appPassword,
        appTenantId: credentials.appTenantId,
        appType: credentials.appType || "MultiTenant",
      });
      // Teams adapter creation validates credentials format; no simple ping API
      jsonResponse(res, 200, { valid: true, message: "Adapter created successfully. Verify messaging endpoint is configured in Azure." });
    } else if (platform === "mattermost") {
      const baseUrl = (credentials.baseUrl || credentials.server_url || "").replace(/\/+$/, "");
      const botToken = credentials.botToken || credentials.bot_token || "";
      if (!baseUrl || !botToken) {
        jsonResponse(res, 200, { valid: false, error: "Server URL and bot token are required" });
        return;
      }
      const mmResp = await fetch(`${baseUrl}/api/v4/users/me`, {
        headers: { Authorization: `Bearer ${botToken}` },
      });
      if (mmResp.ok) {
        const me = await mmResp.json() as { username?: string };
        jsonResponse(res, 200, { valid: true, message: `Connected as @${me.username || "bot"}` });
      } else if (mmResp.status === 401) {
        jsonResponse(res, 200, { valid: false, error: "Invalid bot token" });
      } else {
        jsonResponse(res, 200, { valid: false, error: `Mattermost API returned ${mmResp.status}` });
      }
    } else if (platform === "telegram") {
      const { createTelegramAdapter } = await import("@chat-adapter/telegram" as any);
      // Verify adapter can be constructed (validates config shape)
      createTelegramAdapter({
        botToken: credentials.botToken,
        secretToken: credentials.secretToken,
      });
      // Validate token by calling Telegram getMe API
      const resp = await fetch(`https://api.telegram.org/bot${credentials.botToken}/getMe`);
      const data = await resp.json() as { ok: boolean; description?: string };
      if (data.ok) {
        jsonResponse(res, 200, { valid: true });
      } else {
        jsonResponse(res, 200, { valid: false, error: data.description || "Invalid bot token" });
      }
    } else {
      jsonResponse(res, 400, { valid: false, error: `Unknown platform: ${platform}` });
    }
  } catch (err) {
    console.error(`Bridge: validateAdapter(${platform}) error:`, err);
    jsonResponse(res, 200, { valid: false, error: String(err) });
  }
}

// ── Connection-scoped route helpers ─────────────────────────────────────

async function handleConnectionRoute(
  _req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  connectionId: string,
  handler: (bridge: PlatformBridge) => Promise<void>,
): Promise<void> {
  try {
    const result = getBridgeByConnectionId(chatManager, connectionId);
    if (!result) {
      jsonResponse(res, 404, { error: `Connection "${connectionId}" not found`, code: "NOT_FOUND" });
      return;
    }
    await handler(result.bridge);
  } catch (err) {
    const classified = classifyPlatformError(err);
    // Expected "not found" errors during multi-workspace probing — log briefly, not full stack
    if (classified.status === 404) {
      console.warn(`Bridge: connection route (${connectionId}): ${(err as any)?.data?.error || err}`);
    } else {
      console.error(`Bridge: connection route error (${connectionId}):`, err);
    }
    jsonResponse(res, classified.status, { error: String(err), code: classified.code });
  }
}

async function handleConnectionChannels(
  _req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  connectionId: string,
): Promise<void> {
  try {
    const result = getBridgeByConnectionId(chatManager, connectionId);
    if (!result) {
      jsonResponse(res, 404, { error: `Connection "${connectionId}" not found`, code: "NOT_FOUND" });
      return;
    }
    const channels = await result.bridge.listChannels();
    jsonResponse(res, 200, { channels });
  } catch (err) {
    console.error(`Bridge: listChannels error (connection ${connectionId}):`, err);
    const classified = classifyPlatformError(err);
    jsonResponse(res, classified.status, { error: String(err), code: classified.code });
  }
}

async function handlePlatformChannelsAggregated(
  _req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  platform: string,
): Promise<void> {
  try {
    const adapters = chatManager.getAdaptersByPlatform(platform);
    if (adapters.length === 0) {
      jsonResponse(res, 404, { error: `Platform "${platform}" not connected`, code: "NOT_FOUND" });
      return;
    }

    const allChannels: (NormalizedChannel & { connection_id: string })[] = [];
    for (const { connectionId, adapter } of adapters) {
      const bridge = getOrCreateBridge(platform, connectionId, adapter);
      if (!bridge) continue;
      try {
        const channels = await bridge.listChannels();
        for (const ch of channels) {
          allChannels.push({ ...ch, connection_id: connectionId });
        }
      } catch (err) {
        console.error(`Bridge: listChannels error for ${platform}:${connectionId}:`, err);
      }
    }
    jsonResponse(res, 200, { channels: allChannels });
  } catch (err) {
    console.error(`Bridge: aggregated listChannels error for ${platform}:`, err);
    const classified = classifyPlatformError(err);
    jsonResponse(res, classified.status, { error: String(err), code: classified.code });
  }
}

// ── Route registration ──────────────────────────────────────────────────────

export function registerBridgeRoutes(
  chatManager: ChatManager,
  lazySyncFn?: () => Promise<boolean>,
): (req: IncomingMessage, res: ServerResponse) => Promise<boolean> {
  // Run the production fail-fast / local-dev loud-warning check once at
  // wiring time. Moved out of module-load so tests can import the file.
  assertBridgeAuthReady();

  // Subscribe to adapter rebuilds to clear the bridge singleton cache.
  // This ensures stale adapter references are never reused after unregister/reregister.
  chatManager.onRebuild(() => {
    clearBridgeCache();
  });

  return async (req: IncomingMessage, res: ServerResponse): Promise<boolean> => {
    const url = req.url || "";

    if (!url.startsWith("/bridge/")) return false;
    if (!checkAuth(req, res)) return true;

    // Lazy sync: if the bot has no adapters, attempt recovery before handling
    if (lazySyncFn && chatManager.adapterCount() === 0) {
      await lazySyncFn();
    }

    // POST /bridge/adapters — register adapter
    if (req.method === "POST" && url === "/bridge/adapters") {
      await handleRegisterAdapter(req, res, chatManager);
      return true;
    }

    // GET /bridge/adapters — list adapters
    if (req.method === "GET" && url === "/bridge/adapters") {
      jsonResponse(res, 200, { adapters: chatManager.listAdapters() });
      return true;
    }

    // POST /bridge/adapters/:platform/validate — validate credentials
    const validateMatch = url.match(/^\/bridge\/adapters\/([^/]+)\/validate$/);
    if (req.method === "POST" && validateMatch) {
      await handleValidateAdapter(req, res, validateMatch[1]);
      return true;
    }

    // DELETE /bridge/adapters/:platform — unregister adapter
    const adapterMatch = url.match(/^\/bridge\/adapters\/([^/]+)$/);
    if (req.method === "DELETE" && adapterMatch) {
      await handleUnregisterAdapter(req, res, chatManager, adapterMatch[1]);
      return true;
    }

    // ── Connection-scoped routes ────────────────────────────────────────────

    // GET /bridge/connections/:connId/channels
    const connChannelsMatch = url.match(/^\/bridge\/connections\/([^/]+)\/channels(\?|$)/);
    if (req.method === "GET" && connChannelsMatch) {
      await handleConnectionChannels(req, res, chatManager, connChannelsMatch[1]);
      return true;
    }

    // GET /bridge/connections/:connId/channels/:id/threads/:tid/messages
    const connThreadMatch = url.match(
      /^\/bridge\/connections\/([^/]+)\/channels\/([^/]+)\/threads\/([^/]+)\/messages/,
    );
    if (req.method === "GET" && connThreadMatch) {
      await handleConnectionRoute(req, res, chatManager, connThreadMatch[1], async (bridge) => {
        const messages = await bridge.getThreadMessages(connThreadMatch[2], connThreadMatch[3]);
        jsonResponse(res, 200, { messages });
      });
      return true;
    }

    // GET /bridge/connections/:connId/channels/:id/count
    const connCountMatch = url.match(/^\/bridge\/connections\/([^/]+)\/channels\/([^/]+)\/count$/);
    if (req.method === "GET" && connCountMatch) {
      await handleConnectionRoute(req, res, chatManager, connCountMatch[1], async (bridge) => {
        const count = await bridge.getMessageCount(connCountMatch[2]);
        jsonResponse(res, 200, { count });
      });
      return true;
    }

    // GET /bridge/connections/:connId/channels/:id/messages
    const connMessagesMatch = url.match(/^\/bridge\/connections\/([^/]+)\/channels\/([^/]+)\/messages/);
    if (req.method === "GET" && connMessagesMatch) {
      await handleConnectionRoute(req, res, chatManager, connMessagesMatch[1], async (bridge) => {
        const query = parseQuery(req.url || "");
        const limit = Math.min(parseInt(query.get("limit") || "100", 10), 500);
        const since = query.get("since") ?? undefined;
        const before = query.get("before") ?? undefined;
        const order = query.get("order") ?? "desc";
        const messages = await bridge.getMessages(connMessagesMatch[2], { limit, since, before, order });
        jsonResponse(res, 200, { messages });
      });
      return true;
    }

    // GET /bridge/connections/:connId/channels/:id
    const connChannelMatch = url.match(/^\/bridge\/connections\/([^/]+)\/channels\/([^/]+)$/);
    if (req.method === "GET" && connChannelMatch) {
      await handleConnectionRoute(req, res, chatManager, connChannelMatch[1], async (bridge) => {
        const channel = await bridge.getChannel(connChannelMatch[2]);
        jsonResponse(res, 200, channel);
      });
      return true;
    }

    // ── Platform-prefixed routes ──────────────────────────────────────────

    // GET /bridge/platforms/:platform/channels
    const platformChannelsMatch = url.match(/^\/bridge\/platforms\/([^/]+)\/channels(\?|$)/);
    if (req.method === "GET" && platformChannelsMatch) {
      await handlePlatformChannelsAggregated(req, res, chatManager, platformChannelsMatch[1]);
      return true;
    }

    // GET /bridge/platforms/:platform/channels/:id/threads/:tid/messages
    const platformThreadMatch = url.match(
      /^\/bridge\/platforms\/([^/]+)\/channels\/([^/]+)\/threads\/([^/]+)\/messages/,
    );
    if (req.method === "GET" && platformThreadMatch) {
      await handleGetThreadMessages(req, res, chatManager, platformThreadMatch[2], platformThreadMatch[3], platformThreadMatch[1]);
      return true;
    }

    // GET /bridge/platforms/:platform/channels/:id/messages
    const platformMessagesMatch = url.match(/^\/bridge\/platforms\/([^/]+)\/channels\/([^/]+)\/messages/);
    if (req.method === "GET" && platformMessagesMatch) {
      await handleGetMessages(req, res, chatManager, platformMessagesMatch[2], platformMessagesMatch[1]);
      return true;
    }

    // GET /bridge/platforms/:platform/channels/:id
    const platformChannelMatch = url.match(/^\/bridge\/platforms\/([^/]+)\/channels\/([^/]+)$/);
    if (req.method === "GET" && platformChannelMatch) {
      await handleGetChannel(req, res, chatManager, platformChannelMatch[2], platformChannelMatch[1]);
      return true;
    }

    // GET /bridge/platforms/:platform/files?url=...&connection_id=...
    const platformFilesMatch = url.match(/^\/bridge\/platforms\/([^/]+)\/files/);
    if (req.method === "GET" && platformFilesMatch) {
      const fileQuery = parseQuery(url);
      const fileUrl = fileQuery.get("url");
      if (fileUrl) {
        const connId = fileQuery.get("connection_id") || undefined;
        await handleFileProxy(req, res, chatManager, fileUrl, platformFilesMatch[1], connId);
        return true;
      }
    }

    // Legacy routes (aggregate across all adapters for backward compatibility)

    // GET /bridge/channels
    if (req.method === "GET" && url.match(/^\/bridge\/channels(\?|$)/)) {
      await handleListChannels(req, res, chatManager);
      return true;
    }

    // GET /bridge/channels/:id/threads/:tid/messages
    const threadMatch = url.match(
      /^\/bridge\/channels\/([^/]+)\/threads\/([^/]+)\/messages/,
    );
    if (req.method === "GET" && threadMatch) {
      await handleGetThreadMessages(req, res, chatManager, threadMatch[1], threadMatch[2]);
      return true;
    }

    // GET /bridge/channels/:id/count
    const countMatch = url.match(/^\/bridge\/channels\/([^/]+)\/count$/);
    if (req.method === "GET" && countMatch) {
      await handleGetMessageCount(req, res, chatManager, countMatch[1]);
      return true;
    }

    // GET /bridge/channels/:id/messages
    const messagesMatch = url.match(/^\/bridge\/channels\/([^/]+)\/messages/);
    if (req.method === "GET" && messagesMatch) {
      await handleGetMessages(req, res, chatManager, messagesMatch[1]);
      return true;
    }

    // GET /bridge/channels/:id
    const channelMatch = url.match(/^\/bridge\/channels\/([^/]+)$/);
    if (req.method === "GET" && channelMatch) {
      await handleGetChannel(req, res, chatManager, channelMatch[1]);
      return true;
    }

    // GET /bridge/files?url=...&connection_id=...
    if (req.method === "GET" && url.startsWith("/bridge/files")) {
      console.log("Bridge: /bridge/files route matched, url:", url.slice(0, 80));
      const fileQuery = parseQuery(url);
      const fileUrl = fileQuery.get("url");
      const connId = fileQuery.get("connection_id") || undefined;
      console.log("Bridge: parsed fileUrl:", fileUrl?.slice(0, 60), "connection_id:", connId || "(auto-detect)");
      if (fileUrl) {
        await handleFileProxy(req, res, chatManager, fileUrl, undefined, connId);
        return true;
      }
    }

    jsonResponse(res, 404, { error: "Bridge endpoint not found", code: "NOT_FOUND" });
    return true;
  };
}
