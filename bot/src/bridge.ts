/**
 * Bridge REST API — exposes Chat SDK fetch capabilities to the Python backend.
 *
 * The bot service is the single gateway for all platform communication.
 * These endpoints let the Python backend fetch messages, channels, and threads
 * without needing platform-specific SDKs.
 */

import type { IncomingMessage, ServerResponse } from "node:http";
import type { SlackAdapter } from "@chat-adapter/slack";
import { cleanSlackMrkdwn } from "./slack-mrkdwn.js";
import type { ChatManager } from "./chat-manager.js";

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
}

interface PlatformBridge {
  listChannels(): Promise<NormalizedChannel[]>;
  getChannel(id: string): Promise<NormalizedChannel>;
  getMessages(id: string, opts: GetMessagesOpts): Promise<NormalizedMessage[]>;
  getThreadMessages(channelId: string, threadId: string): Promise<NormalizedMessage[]>;
  proxyFile(url: string): Promise<{ contentType: string; buffer: Buffer }>;
  resolveUser(userId: string): Promise<{ name: string; image: string | null }>;
}

// ── Auth ────────────────────────────────────────────────────────────────────

const BRIDGE_API_KEY = process.env.BRIDGE_API_KEY || "";

function checkAuth(req: IncomingMessage, res: ServerResponse): boolean {
  if (!BRIDGE_API_KEY) return true; // No key configured = no auth required (dev mode)

  const authHeader = req.headers.authorization || "";
  if (authHeader !== `Bearer ${BRIDGE_API_KEY}`) {
    res.writeHead(401, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "Unauthorized", code: "AUTH_FAILED" }));
    return false;
  }
  return true;
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
          })),
          ...(msg.attachments || [])
            .filter((a: any) => a.image_url && !a.from_url && !a.original_url)
            .map((a: any) => ({
              type: "image" as const,
              url: a.image_url,
              name: a.title || a.fallback || "Image",
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

    messages.reverse();
    return messages;
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

  async proxyFile(fileUrl: string): Promise<{ contentType: string; buffer: Buffer }> {
    const decodedUrl = decodeURIComponent(fileUrl);
    const token = (this.adapter as any).defaultBotToken || (this.adapter as any).getToken();

    let response = await fetch(decodedUrl, {
      headers: { Authorization: `Bearer ${token}` },
    });

    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("text/html") && decodedUrl.includes("files-pri")) {
      console.log("Bridge: fileProxy got HTML, trying files.sharedPublicURL fallback");
      const match = decodedUrl.match(/files-pri\/[^/]+-([^/]+)\//);
      if (match) {
        const fileId = `F${match[1]}`;
        try {
          const fileInfo = await (this.adapter as any).client.files.info({ file: fileId });
          const downloadUrl = fileInfo.file?.url_private_download || fileInfo.file?.url_private;
          if (downloadUrl) {
            response = await fetch(downloadUrl, {
              headers: { Authorization: `Bearer ${token}` },
            });
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
    const buffer = Buffer.from(await response.arrayBuffer());
    return { contentType: finalContentType, buffer };
  }
}

// ── DiscordBridge ─────────────────────────────────────────────────────────────

class DiscordBridge implements PlatformBridge {
  private adapter: unknown;

  constructor(adapter: unknown) {
    this.adapter = adapter;
  }

  async resolveUser(userId: string): Promise<{ name: string; image: string | null }> {
    try {
      const client = (this.adapter as any).client;
      const user = await client.users.fetch(userId);
      return {
        name: user.globalName || user.username || userId,
        image: user.displayAvatarURL?.() || null,
      };
    } catch {
      return { name: userId, image: null };
    }
  }

  async listChannels(): Promise<NormalizedChannel[]> {
    try {
      const client = (this.adapter as any).client;
      const guilds = client.guilds?.cache ?? new Map();
      const channels: NormalizedChannel[] = [];

      for (const guild of guilds.values()) {
        const guildChannels = guild.channels?.cache ?? new Map();
        for (const ch of guildChannels.values()) {
          if ((ch as any).isTextBased?.()) {
            channels.push({
              channel_id: (ch as any).id,
              name: (ch as any).name || "",
              platform: "discord",
              is_member: true,
              member_count: (ch as any).members?.size ?? null,
              topic: (ch as any).topic ?? null,
              purpose: null,
            });
          }
        }
      }

      return channels;
    } catch (err) {
      console.error("DiscordBridge: listChannels error:", err);
      return [];
    }
  }

  async getChannel(id: string): Promise<NormalizedChannel> {
    const client = (this.adapter as any).client;
    const ch = await client.channels.fetch(id);
    return {
      channel_id: id,
      name: (ch as any).name || "",
      platform: "discord",
      is_member: true,
      member_count: (ch as any).members?.size ?? null,
      topic: (ch as any).topic ?? null,
      purpose: null,
    };
  }

  async getMessages(channelId: string, opts: GetMessagesOpts): Promise<NormalizedMessage[]> {
    const client = (this.adapter as any).client;
    const channel = await client.channels.fetch(channelId);
    const fetchOpts: Record<string, unknown> = { limit: Math.min(opts.limit, 100) };

    const rawMessages = await (channel as any).messages.fetch(fetchOpts);
    const messages: NormalizedMessage[] = [];

    for (const msg of rawMessages.values()) {
      const m = msg as any;
      messages.push({
        content: m.content || "",
        author: m.author?.id || "unknown",
        author_name: m.author?.globalName || m.author?.username || "unknown",
        author_image: m.author?.displayAvatarURL?.() || null,
        platform: "discord",
        channel_id: channelId,
        channel_name: (channel as any).name || "",
        message_id: m.id,
        timestamp: m.createdAt?.toISOString() || new Date().toISOString(),
        thread_id: m.reference?.messageId ?? null,
        attachments: (m.attachments ? [...m.attachments.values()] : []).map((a: any) => ({
          type: a.contentType?.startsWith("image/") ? "image" : "file",
          url: a.url,
          name: a.name,
        })),
        reactions: (m.reactions ? [...m.reactions.cache.values()] : []).map((r: any) => ({
          name: r.emoji?.name || "",
          count: r.count || 0,
        })),
        reply_count: 0,
        is_bot: m.author?.bot ?? false,
        subtype: null,
        links: [],
      });
    }

    messages.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
    return messages;
  }

  async getThreadMessages(channelId: string, threadId: string): Promise<NormalizedMessage[]> {
    // Discord threads are channels themselves — fetch the thread channel
    return this.getMessages(threadId, { limit: 100 });
  }

  async proxyFile(url: string): Promise<{ contentType: string; buffer: Buffer }> {
    // Discord CDN URLs are public (time-limited), no auth needed
    const response = await fetch(decodeURIComponent(url));
    if (!response.ok) {
      throw new Error(`Failed to fetch Discord file: ${response.status}`);
    }
    const contentType = response.headers.get("content-type") || "application/octet-stream";
    const buffer = Buffer.from(await response.arrayBuffer());
    return { contentType, buffer };
  }
}

// ── TeamsBridge ──────────────────────────────────────────────────────────────
// Teams bots work via real-time webhooks (Azure Bot Service), not REST data pull.
// Channel listing and message history require Microsoft Graph API setup beyond
// the adapter scope — these methods return empty stubs.

class TeamsBridge implements PlatformBridge {
  private adapter: unknown;

  constructor(adapter: unknown) {
    this.adapter = adapter;
  }

  async resolveUser(_userId: string): Promise<{ name: string; image: string | null }> {
    return { name: _userId, image: null };
  }

  async listChannels(): Promise<NormalizedChannel[]> {
    console.warn("TeamsBridge: listChannels() is not supported — Teams channel listing requires Microsoft Graph API setup");
    return [];
  }

  async getChannel(id: string): Promise<NormalizedChannel> {
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

  async getMessages(_channelId: string, _opts: GetMessagesOpts): Promise<NormalizedMessage[]> {
    console.warn("TeamsBridge: getMessages() is not supported — Teams message history access requires Microsoft Graph API setup");
    return [];
  }

  async getThreadMessages(_channelId: string, _threadId: string): Promise<NormalizedMessage[]> {
    return [];
  }

  async proxyFile(url: string): Promise<{ contentType: string; buffer: Buffer }> {
    const response = await fetch(decodeURIComponent(url));
    if (!response.ok) {
      throw new Error(`Failed to fetch Teams file: ${response.status}`);
    }
    const contentType = response.headers.get("content-type") || "application/octet-stream";
    const buffer = Buffer.from(await response.arrayBuffer());
    return { contentType, buffer };
  }
}

// ── TelegramBridge ────────────────────────────────────────────────────────────
// Telegram bots are event-driven — they receive messages via webhook but cannot
// pull message history or list group chats. These methods return empty stubs.

class TelegramBridge implements PlatformBridge {
  private adapter: unknown;

  constructor(adapter: unknown) {
    this.adapter = adapter;
  }

  async resolveUser(_userId: string): Promise<{ name: string; image: string | null }> {
    return { name: _userId, image: null };
  }

  async listChannels(): Promise<NormalizedChannel[]> {
    console.warn("TelegramBridge: listChannels() is not supported — Telegram bots have no channel listing API");
    return [];
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

  async getMessages(_channelId: string, _opts: GetMessagesOpts): Promise<NormalizedMessage[]> {
    console.warn("TelegramBridge: getMessages() is not supported — Telegram bots cannot fetch message history");
    return [];
  }

  async getThreadMessages(_channelId: string, _threadId: string): Promise<NormalizedMessage[]> {
    return [];
  }

  async proxyFile(url: string): Promise<{ contentType: string; buffer: Buffer }> {
    const response = await fetch(decodeURIComponent(url));
    if (!response.ok) {
      throw new Error(`Failed to fetch Telegram file: ${response.status}`);
    }
    const contentType = response.headers.get("content-type") || "application/octet-stream";
    const buffer = Buffer.from(await response.arrayBuffer());
    return { contentType, buffer };
  }
}

// ── Bridge factory ───────────────────────────────────────────────────────────

function createBridgeForPlatform(platform: string, adapter: unknown): PlatformBridge | null {
  if (platform === "slack") return new SlackBridge(adapter as SlackAdapter);
  if (platform === "discord") return new DiscordBridge(adapter);
  if (platform === "teams") return new TeamsBridge(adapter);
  if (platform === "telegram") return new TelegramBridge(adapter);
  return null;
}

function getBridge(chatManager: ChatManager, platform: string, connectionId?: string): PlatformBridge | null {
  if (connectionId) {
    const entry = chatManager.getAdapterByConnectionId(connectionId);
    if (!entry) return null;
    return createBridgeForPlatform(entry.platform, entry.adapter);
  }
  const adapter = chatManager.getAdapter(platform);
  if (!adapter) return null;
  return createBridgeForPlatform(platform, adapter);
}

function getBridgeByConnectionId(chatManager: ChatManager, connectionId: string): { platform: string; bridge: PlatformBridge } | null {
  const entry = chatManager.getAdapterByConnectionId(connectionId);
  if (!entry) return null;
  const bridge = createBridgeForPlatform(entry.platform, entry.adapter);
  if (!bridge) return null;
  return { platform: entry.platform, bridge };
}

function getFirstBridge(chatManager: ChatManager): { platform: string; bridge: PlatformBridge } | null {
  const adapters = chatManager.listAdapters();
  for (const { platform, connectionId } of adapters) {
    const bridge = getBridge(chatManager, platform, connectionId);
    if (bridge) return { platform, bridge };
  }
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
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
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
    let bridge: PlatformBridge | null = null;
    if (platform) {
      bridge = getBridge(chatManager, platform);
    } else {
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

    let bridge: PlatformBridge | null = null;
    if (platform) {
      bridge = getBridge(chatManager, platform);
    } else {
      const first = getFirstBridge(chatManager);
      bridge = first?.bridge ?? null;
    }

    if (!bridge) {
      jsonResponse(res, 503, { error: "No platform adapters connected", code: "NO_ADAPTER" });
      return;
    }

    const messages = await bridge.getMessages(channelId, { limit, since });
    jsonResponse(res, 200, { messages });
  } catch (err) {
    console.error("Bridge: getMessages error:", err);
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
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
    let bridge: PlatformBridge | null = null;
    if (platform) {
      bridge = getBridge(chatManager, platform);
    } else {
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
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
  }
}

async function handleFileProxy(
  req: IncomingMessage,
  res: ServerResponse,
  chatManager: ChatManager,
  fileUrl: string,
  platform?: string,
): Promise<void> {
  try {
    let bridge: PlatformBridge | null = null;
    if (platform) {
      bridge = getBridge(chatManager, platform);
    } else {
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
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
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
      const { createDiscordAdapter } = await import("@chat-adapter/discord" as any);
      const tempAdapter = createDiscordAdapter({
        botToken: credentials.botToken,
        publicKey: credentials.publicKey,
        applicationId: credentials.applicationId,
      });
      // Discord.js REST — fetch current user to validate token
      const rest = (tempAdapter as any).rest || (tempAdapter as any).client?.rest;
      if (rest) {
        await rest.get("/users/@me");
        jsonResponse(res, 200, { valid: true });
      } else {
        jsonResponse(res, 200, { valid: true }); // Adapter created without error
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
    console.error(`Bridge: connection route error (${connectionId}):`, err);
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
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
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
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
      const bridge = createBridgeForPlatform(platform, adapter);
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
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
  }
}

// ── Route registration ──────────────────────────────────────────────────────

export function registerBridgeRoutes(
  chatManager: ChatManager,
): (req: IncomingMessage, res: ServerResponse) => Promise<boolean> {
  return async (req: IncomingMessage, res: ServerResponse): Promise<boolean> => {
    const url = req.url || "";

    if (!url.startsWith("/bridge/")) return false;
    if (!checkAuth(req, res)) return true;

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

    // GET /bridge/connections/:connId/channels/:id/messages
    const connMessagesMatch = url.match(/^\/bridge\/connections\/([^/]+)\/channels\/([^/]+)\/messages/);
    if (req.method === "GET" && connMessagesMatch) {
      await handleConnectionRoute(req, res, chatManager, connMessagesMatch[1], async (bridge) => {
        const query = parseQuery(req.url || "");
        const limit = Math.min(parseInt(query.get("limit") || "100", 10), 500);
        const since = query.get("since") ?? undefined;
        const messages = await bridge.getMessages(connMessagesMatch[2], { limit, since });
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

    // GET /bridge/platforms/:platform/files?url=...
    const platformFilesMatch = url.match(/^\/bridge\/platforms\/([^/]+)\/files/);
    if (req.method === "GET" && platformFilesMatch) {
      const fileQuery = parseQuery(url);
      const fileUrl = fileQuery.get("url");
      if (fileUrl) {
        await handleFileProxy(req, res, chatManager, fileUrl, platformFilesMatch[1]);
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

    // GET /bridge/files?url=...
    if (req.method === "GET" && url.startsWith("/bridge/files")) {
      console.log("Bridge: /bridge/files route matched, url:", url.slice(0, 80));
      const fileQuery = parseQuery(url);
      const fileUrl = fileQuery.get("url");
      console.log("Bridge: parsed fileUrl:", fileUrl?.slice(0, 60));
      if (fileUrl) {
        await handleFileProxy(req, res, chatManager, fileUrl);
        return true;
      }
    }

    jsonResponse(res, 404, { error: "Bridge endpoint not found", code: "NOT_FOUND" });
    return true;
  };
}
