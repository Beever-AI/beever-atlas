/**
 * ChatManager — adapter registry and Chat instance lifecycle manager.
 *
 * ChatSDK's `Chat` class has immutable adapters (private readonly). This class
 * wraps Chat to support runtime adapter registration/unregistration by recreating
 * the Chat instance with the new adapter set, re-registering all event handlers,
 * and buffering webhooks during the transition window.
 *
 * Adapters are keyed by composite key `{platform}:{connectionId}` to support
 * multiple connections per platform (e.g., two Slack workspaces).
 */

import { Chat } from "chat";
import { createSlackAdapter } from "@chat-adapter/slack";
import { createRedisState } from "@chat-adapter/state-redis";

// ── Types ────────────────────────────────────────────────────────────────────

interface AdapterEntry {
  platform: string;
  connectionId: string;
  config: Record<string, string>;
}

export interface AdapterInfo {
  platform: string;
  connectionId: string;
  status: string;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function compositeKey(platform: string, connectionId: string): string {
  return `${platform}:${connectionId}`;
}

// ── ChatManager ───────────────────────────────────────────────────────────────

export class ChatManager {
  private currentBot: Chat | null = null;
  private adapters: Map<string, AdapterEntry> = new Map();
  private registerHandlers: (bot: Chat) => void;
  private redisUrl: string;
  private transitioning: boolean = false;

  constructor(redisUrl: string, registerHandlers: (bot: Chat) => void) {
    this.redisUrl = redisUrl;
    this.registerHandlers = registerHandlers;
  }

  /**
   * Register a platform adapter with credentials. Triggers a Chat rebuild.
   * When connectionId is omitted, the platform name is used as fallback
   * (backward compat for env-sourced connections).
   */
  async register(
    platform: string,
    credentials: Record<string, string>,
    connectionId?: string,
  ): Promise<void> {
    const connId = connectionId || platform;
    const key = compositeKey(platform, connId);
    this.adapters.set(key, { platform, connectionId: connId, config: credentials });
    await this.rebuild();
  }

  /**
   * Unregister a platform adapter. Triggers a Chat rebuild.
   * When connectionId is omitted, uses platform as fallback key.
   */
  async unregister(platform: string, connectionId?: string): Promise<void> {
    const connId = connectionId || platform;
    const key = compositeKey(platform, connId);
    this.adapters.delete(key);
    await this.rebuild();
  }

  /**
   * Unregister an adapter by connection ID alone (searches all entries).
   */
  async unregisterByConnectionId(connectionId: string): Promise<boolean> {
    for (const [key, entry] of this.adapters.entries()) {
      if (entry.connectionId === connectionId) {
        this.adapters.delete(key);
        await this.rebuild();
        return true;
      }
    }
    return false;
  }

  /**
   * Rebuild the Chat instance from current adapter registry.
   * Awaits shutdown of the old instance, creates fresh adapter instances,
   * then re-registers all event handlers.
   */
  async rebuild(): Promise<void> {
    this.transitioning = true;

    if (this.currentBot) {
      try {
        await this.currentBot.shutdown();
      } catch (err: unknown) {
        console.warn("ChatManager: error during shutdown:", err);
      }
      this.currentBot = null;
    }

    if (this.adapters.size === 0) {
      console.log("ChatManager: no adapters registered, bot is offline");
      this.transitioning = false;
      return;
    }

    // Build fresh adapter instances from stored configs.
    // The composite key is used as the Chat SDK adapter key.
    // Platform is extracted from the entry for factory selection.
    const adapterInstances: Record<string, unknown> = {};

    for (const [key, entry] of this.adapters.entries()) {
      try {
        if (entry.platform === "slack") {
          adapterInstances[key] = createSlackAdapter({
            botToken: entry.config.botToken,
            signingSecret: entry.config.signingSecret,
          });
        } else if (entry.platform === "discord") {
          const { createDiscordAdapter } = require("@chat-adapter/discord");
          const discordOpts: Record<string, unknown> = {
            botToken: entry.config.botToken,
            publicKey: entry.config.publicKey,
            applicationId: entry.config.applicationId,
          };
          if (entry.config.mentionRoleIds) {
            discordOpts.mentionRoleIds = String(entry.config.mentionRoleIds).split(",").map((s: string) => s.trim()).filter(Boolean);
          }
          adapterInstances[key] = createDiscordAdapter(discordOpts);
        } else if (entry.platform === "teams") {
          const { createTeamsAdapter } = require("@chat-adapter/teams");
          adapterInstances[key] = createTeamsAdapter({
            appId: entry.config.appId,
            appPassword: entry.config.appPassword,
            appTenantId: entry.config.appTenantId,
            appType: entry.config.appType,
          });
        } else if (entry.platform === "telegram") {
          const { createTelegramAdapter } = require("@chat-adapter/telegram");
          adapterInstances[key] = createTelegramAdapter({
            botToken: entry.config.botToken,
            secretToken: entry.config.secretToken,
          });
        } else {
          console.warn(`ChatManager: unknown platform "${entry.platform}", skipping`);
        }
      } catch (err) {
        console.error(`ChatManager: failed to create adapter for "${key}":`, err);
      }
    }

    if (Object.keys(adapterInstances).length === 0) {
      console.warn("ChatManager: no valid adapters could be created");
      this.transitioning = false;
      return;
    }

    const newBot = new Chat({
      userName: "beever",
      adapters: adapterInstances as Record<string, import("chat").Adapter>,
      state: createRedisState({ url: this.redisUrl }),
    });

    this.registerHandlers(newBot);
    this.currentBot = newBot;
    this.transitioning = false;

    console.log(`ChatManager: bot rebuilt with adapters: ${Object.keys(adapterInstances).join(", ")}`);
  }

  getCurrentBot(): Chat | null {
    return this.currentBot;
  }

  /**
   * Returns the raw adapter instance for a given composite key.
   */
  getAdapter(compositeKeyOrPlatform: string): unknown {
    if (!this.currentBot) return null;
    const adaptersMap = (this.currentBot as any).adapters as Map<string, unknown> | undefined;
    if (!adaptersMap) return null;

    // Try exact composite key first
    const exact = adaptersMap.get(compositeKeyOrPlatform);
    if (exact) return exact;

    // Fallback: find first adapter matching as platform prefix (legacy compat)
    for (const [key, adapter] of adaptersMap.entries()) {
      if (key === compositeKeyOrPlatform || key.startsWith(`${compositeKeyOrPlatform}:`)) {
        return adapter;
      }
    }
    return null;
  }

  /**
   * Look up an adapter by connection ID.
   */
  getAdapterByConnectionId(connectionId: string): { platform: string; connectionId: string; adapter: unknown } | null {
    if (!this.currentBot) return null;
    const adaptersMap = (this.currentBot as any).adapters as Map<string, unknown> | undefined;
    if (!adaptersMap) return null;

    for (const [key, entry] of this.adapters.entries()) {
      if (entry.connectionId === connectionId) {
        const adapter = adaptersMap.get(key);
        if (adapter) {
          return { platform: entry.platform, connectionId: entry.connectionId, adapter };
        }
      }
    }
    return null;
  }

  /**
   * Return all adapters for a given platform.
   */
  getAdaptersByPlatform(platform: string): { compositeKey: string; connectionId: string; adapter: unknown }[] {
    if (!this.currentBot) return [];
    const adaptersMap = (this.currentBot as any).adapters as Map<string, unknown> | undefined;
    if (!adaptersMap) return [];

    const results: { compositeKey: string; connectionId: string; adapter: unknown }[] = [];
    for (const [key, entry] of this.adapters.entries()) {
      if (entry.platform === platform) {
        const adapter = adaptersMap.get(key);
        if (adapter) {
          results.push({ compositeKey: key, connectionId: entry.connectionId, adapter });
        }
      }
    }
    return results;
  }

  /**
   * Get the composite key for a connection ID.
   */
  getCompositeKeyForConnection(connectionId: string): string | null {
    for (const [key, entry] of this.adapters.entries()) {
      if (entry.connectionId === connectionId) {
        return key;
      }
    }
    return null;
  }

  listAdapters(): AdapterInfo[] {
    const result: AdapterInfo[] = [];
    for (const [key, entry] of this.adapters.entries()) {
      const adapterInstance = this.getAdapter(key);
      result.push({
        platform: entry.platform,
        connectionId: entry.connectionId,
        status: adapterInstance ? "connected" : "error",
      });
    }
    return result;
  }

  isTransitioning(): boolean {
    return this.transitioning;
  }
}
