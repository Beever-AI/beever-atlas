/**
 * Bridge REST API — exposes Chat SDK fetch capabilities to the Python backend.
 *
 * The bot service is the single gateway for all platform communication.
 * These endpoints let the Python backend fetch messages, channels, and threads
 * without needing platform-specific SDKs.
 */

import type { IncomingMessage, ServerResponse } from "node:http";
import type { Chat } from "chat";
import type { SlackAdapter } from "@chat-adapter/slack";
import { cleanSlackMrkdwn } from "./slack-mrkdwn.js";

// ── Types ───────────────────────────────────────────────────────────────────

interface NormalizedMessage {
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

interface NormalizedChannel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  topic: string | null;
  purpose: string | null;
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

function detectPlatform(bot: Chat): string {
  // Return the first registered adapter name as default platform
  const adapterNames = Object.keys((bot as any)._adapters || {});
  return adapterNames[0] || "slack";
}

// ── User profile cache (module-level, persists across requests) ─────────────

const userProfileCache = new Map<string, { name: string; image: string | null }>();
const USER_LOOKUP_CONCURRENCY = 8;

async function resolveUser(
  slackAdapter: SlackAdapter,
  userId: string,
): Promise<{ name: string; image: string | null }> {
  if (userProfileCache.has(userId)) return userProfileCache.get(userId)!;
  try {
    const result = await (slackAdapter as any).client.users.info({ user: userId });
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

// ── Route handlers ──────────────────────────────────────────────────────────

async function handleListChannels(
  _req: IncomingMessage,
  res: ServerResponse,
  bot: Chat,
  slackAdapter: SlackAdapter,
): Promise<void> {
  try {
    // Chat SDK has no listChannels() — use raw adapter API
    const channels: NormalizedChannel[] = [];
    let cursor: string | undefined;

    do {
      const result = await (slackAdapter as any).client.conversations.list({
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

    jsonResponse(res, 200, { channels });
  } catch (err) {
    console.error("Bridge: listChannels error:", err);
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
  }
}

async function handleGetChannel(
  _req: IncomingMessage,
  res: ServerResponse,
  bot: Chat,
  slackAdapter: SlackAdapter,
  channelId: string,
): Promise<void> {
  try {
    const platform = detectPlatform(bot);

    // Use Slack API directly to get channel info including is_member
    const result = await (slackAdapter as any).client.conversations.info({
      channel: channelId,
    });
    const ch = result.channel;

    jsonResponse(res, 200, {
      channel_id: channelId,
      name: ch?.name || "",
      platform,
      is_member: ch?.is_member ?? false,
      member_count: ch?.num_members ?? null,
      topic: ch?.topic?.value ?? null,
      purpose: ch?.purpose?.value ?? null,
    });
  } catch (err) {
    console.error("Bridge: getChannel error:", err);
    jsonResponse(res, 404, { error: `Channel ${channelId} not found`, code: "NOT_FOUND" });
  }
}

async function handleGetMessages(
  req: IncomingMessage,
  res: ServerResponse,
  _bot: Chat,
  channelId: string,
  slackAdapter: SlackAdapter,
): Promise<void> {
  try {
    const query = parseQuery(req.url || "");
    const limit = Math.min(parseInt(query.get("limit") || "100", 10), 500);
    const sinceStr = query.get("since");

    // Use raw Slack API directly instead of Chat SDK's channel.messages iterator.
    // The Chat SDK strips files, attachments, and reactions from the raw event.
    const historyParams: Record<string, unknown> = {
      channel: channelId,
      limit,
    };
    if (sinceStr) {
      // Convert ISO date to Slack epoch timestamp
      const sinceEpoch = new Date(sinceStr).getTime() / 1000;
      historyParams.oldest = String(sinceEpoch);
    }

    const result = await (slackAdapter as any).client.conversations.history(historyParams);
    const rawSlackMessages = result.messages || [];

    // Get channel name via Slack API
    let channelName = "";
    try {
      const chInfo = await (slackAdapter as any).client.conversations.info({ channel: channelId });
      channelName = chInfo.channel?.name || "";
    } catch { /* ignore */ }

    // Resolve user profiles
    const userIds: string[] = [...new Set<string>(
      rawSlackMessages
        .filter((m: any) => m.user && !m.bot_id)
        .map((m: any) => m.user as string),
    )];
    const userMap = new Map<string, { name: string; image: string | null }>();
    for (let i = 0; i < userIds.length; i += USER_LOOKUP_CONCURRENCY) {
      const chunk = userIds.slice(i, i + USER_LOOKUP_CONCURRENCY);
      const resolved = await Promise.all(
        chunk.map(async (uid: string) => [uid, await resolveUser(slackAdapter, uid)] as const),
      );
      for (const [uid, profile] of resolved) {
        userMap.set(uid, profile);
      }
    }

    // Map raw Slack messages to NormalizedMessage
    const messages: NormalizedMessage[] = rawSlackMessages.map((msg: any) => {
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
        // File uploads from Slack
        attachments: [
          ...(msg.files || []).map((f: any) => ({
            type: f.mimetype?.startsWith("image/") ? "image"
                : f.mimetype?.startsWith("video/") ? "video"
                : "file",
            url: f.url_private || f.permalink,
            name: f.name || f.title,
          })),
          // Slack attachment unfurls with standalone images (not link previews)
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
        // Link unfurls from Slack attachments
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

    // Slack returns newest-first; reverse to chronological order
    messages.reverse();

    jsonResponse(res, 200, { messages });
  } catch (err) {
    console.error("Bridge: getMessages error:", err);
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
  }
}

async function handleGetThreadMessages(
  _req: IncomingMessage,
  res: ServerResponse,
  slackAdapter: SlackAdapter,
  channelId: string,
  threadId: string,
): Promise<void> {
  try {
    // Use Slack conversations.replies API directly — O(1) vs O(n) channel iteration
    const result = await (slackAdapter as any).client.conversations.replies({
      channel: channelId,
      ts: threadId,
      limit: 200,
    });

    const rawReplies = result.messages || [];

    // Resolve user profiles for all authors
    const userIds: string[] = [...new Set<string>(
      rawReplies
        .filter((m: any) => m.user && !m.bot_id)
        .map((m: any) => m.user as string),
    )];
    const userMap = new Map<string, { name: string; image: string | null }>();
    for (let i = 0; i < userIds.length; i += USER_LOOKUP_CONCURRENCY) {
      const chunk = userIds.slice(i, i + USER_LOOKUP_CONCURRENCY);
      const resolved = await Promise.all(
        chunk.map(async (uid: string) => [uid, await resolveUser(slackAdapter, uid)] as const),
      );
      for (const [uid, profile] of resolved) {
        userMap.set(uid, profile);
      }
    }

    const messages: NormalizedMessage[] = rawReplies.map((msg: any) => {
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

    jsonResponse(res, 200, { messages });
  } catch (err) {
    console.error("Bridge: getThreadMessages error:", err);
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
  }
}

async function handleFileProxy(
  req: IncomingMessage,
  res: ServerResponse,
  slackAdapter: SlackAdapter,
  fileUrl: string,
): Promise<void> {
  try {
    const decodedUrl = decodeURIComponent(fileUrl);
    const token = (slackAdapter as any).client.token;
    const response = await fetch(decodedUrl, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) {
      jsonResponse(res, response.status, { error: "Failed to fetch file" });
      return;
    }
    const contentType = response.headers.get("content-type") || "application/octet-stream";
    res.writeHead(200, {
      "Content-Type": contentType,
      "Cache-Control": "public, max-age=3600",
    });
    const buffer = Buffer.from(await response.arrayBuffer());
    res.end(buffer);
  } catch (err) {
    console.error("Bridge: fileProxy error:", err);
    jsonResponse(res, 500, { error: String(err), code: "FETCH_ERROR" });
  }
}

// ── Route registration ──────────────────────────────────────────────────────

export function registerBridgeRoutes(
  bot: Chat,
  slackAdapter: SlackAdapter,
): (req: IncomingMessage, res: ServerResponse) => Promise<boolean> {
  return async (req: IncomingMessage, res: ServerResponse): Promise<boolean> => {
    const url = req.url || "";

    if (!url.startsWith("/bridge/")) return false;
    if (!checkAuth(req, res)) return true;

    // GET /bridge/channels
    if (req.method === "GET" && url.match(/^\/bridge\/channels(\?|$)/)) {
      await handleListChannels(req, res, bot, slackAdapter);
      return true;
    }

    // GET /bridge/channels/:id/threads/:tid/messages
    const threadMatch = url.match(
      /^\/bridge\/channels\/([^/]+)\/threads\/([^/]+)\/messages/,
    );
    if (req.method === "GET" && threadMatch) {
      await handleGetThreadMessages(req, res, slackAdapter, threadMatch[1], threadMatch[2]);
      return true;
    }

    // GET /bridge/channels/:id/messages
    const messagesMatch = url.match(/^\/bridge\/channels\/([^/]+)\/messages/);
    if (req.method === "GET" && messagesMatch) {
      await handleGetMessages(req, res, bot, messagesMatch[1], slackAdapter);
      return true;
    }

    // GET /bridge/channels/:id
    const channelMatch = url.match(/^\/bridge\/channels\/([^/]+)$/);
    if (req.method === "GET" && channelMatch) {
      await handleGetChannel(req, res, bot, slackAdapter, channelMatch[1]);
      return true;
    }

    // GET /bridge/files?url=...
    if (req.method === "GET" && url.startsWith("/bridge/files")) {
      const fileQuery = parseQuery(url);
      const fileUrl = fileQuery.get("url");
      if (fileUrl) {
        await handleFileProxy(req, res, slackAdapter, fileUrl);
        return true;
      }
    }

    jsonResponse(res, 404, { error: "Bridge endpoint not found", code: "NOT_FOUND" });
    return true;
  };
}
