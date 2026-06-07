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
import { createDiscordAdapter } from "@chat-adapter/discord";
import { createTeamsAdapter } from "@chat-adapter/teams";
import { createTelegramAdapter } from "@chat-adapter/telegram";
import { createMattermostAdapter } from "chat-adapter-mattermost";
import { createRedisState } from "@chat-adapter/state-redis";
// M6: safeErrorMessage logs only the error message (whitespace-collapsed and
// length-capped) so stack traces / token values never reach container logs or
// log aggregators. Single shared definition lives in http-utils and is reused
// here, in bridge.ts, and in index.ts.
import { safeErrorMessage } from "./http-utils.js";

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
  private rebuildListeners: Array<() => void> = [];
  private rebuildCompleteListeners: Array<() => void> = [];
  /** Maps workspace identifiers to connectionId for URL-based routing.
   *  e.g. Slack team_id "T0APJ2FNUKZ" → connectionId "abc-123" */
  private workspaceIdMap: Map<string, string> = new Map();
  /** Maps connectionId → Slack workspace domain (the subdomain of *.slack.com,
   *  e.g. "beever"). Captured from auth.test's `url` at registration; used to
   *  build clickable message permalinks for citations. Slack only. */
  private workspaceDomainMap: Map<string, string> = new Map();
  /** RES-286 — scheduled adapter recycle timer.
   *  Tears down + rebuilds every adapter periodically to drop accumulated
   *  state (notably the chat-adapter-mattermost ws closures and bridge.ts
   *  module-level user cache). */
  private recycleTimer: ReturnType<typeof setInterval> | null = null;
  /** RES-286 — circuit breaker. If `rebuild()` throws on
   *  `RECYCLE_FAILURE_LIMIT` consecutive scheduled ticks the timer halts so
   *  we don't fill logs with the same error every 6 h. The first failure
   *  on each run is still surfaced; the timer can be re-enabled by another
   *  call to `scheduleAdapterRecycle(...)`. */
  private consecutiveRecycleFailures: number = 0;
  private static readonly RECYCLE_FAILURE_LIMIT = 3;

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
  /** Register a callback invoked whenever adapters are rebuilt (for cache invalidation). */
  onRebuild(listener: () => void): void {
    this.rebuildListeners.push(listener);
  }

  private notifyRebuildListeners(): void {
    for (const fn of this.rebuildListeners) {
      try { fn(); } catch { /* listener errors must not break rebuild */ }
    }
  }

  /** Register a callback invoked AFTER each rebuild completes (success, no-adapters, or error).
   *  Distinct from `onRebuild`, which fires at rebuild START (used by bridge.ts for cache
   *  invalidation). Used by WebhookBuffer to drain buffered requests once the new bot is ready. */
  onRebuildComplete(listener: () => void): void {
    this.rebuildCompleteListeners.push(listener);
  }

  private notifyRebuildCompleteListeners(): void {
    for (const fn of this.rebuildCompleteListeners) {
      try { fn(); } catch { /* listener errors must not break rebuild */ }
    }
  }

  async rebuild(): Promise<void> {
    this.transitioning = true;
    this.notifyRebuildListeners();

    try {
      if (this.currentBot) {
        try {
          await this.currentBot.shutdown();
        } catch (err: unknown) {
          console.warn("ChatManager: error during shutdown:", safeErrorMessage(err));
        }
        this.currentBot = null;
      }

      if (this.adapters.size === 0) {
        console.log("ChatManager: no adapters registered, bot is offline");
        return;
      }

      // Build fresh adapter instances from stored configs.
      // The composite key is used as the Chat SDK adapter key.
      // Platform is extracted from the entry for factory selection.
      // Required credential keys per platform — entries missing any of these are
      // skipped so a single broken connection cannot take down the entire bot.
      const REQUIRED_CREDENTIALS: Record<string, string[]> = {
        // Slack has two valid credential shapes (webhook vs socket mode), so it
        // is validated separately below rather than via this simple AND-list.
        discord: ["botToken", "publicKey", "applicationId"],
        teams: ["appId", "appPassword"],
        telegram: ["botToken"],
        mattermost: ["baseUrl", "botToken"],
      };

      const adapterInstances: Record<string, unknown> = {};

      for (const [key, entry] of this.adapters.entries()) {
        // Slack: botToken is always required, plus EITHER a signingSecret
        // (Events API / webhook mode — needs a public inbound URL) OR an
        // appToken (Socket Mode — outbound WebSocket, no public URL needed).
        let slackMode: "socket" | "webhook" | null = null;
        if (entry.platform === "slack") {
          const plan = planSlackAdapter(entry.config);
          if (!plan.ok) {
            console.warn(`ChatManager: skipping "${key}" — missing required credentials: ${plan.missing.join(", ")}`);
            continue;
          }
          slackMode = plan.mode;
        }

        const required = REQUIRED_CREDENTIALS[entry.platform];
        if (required) {
          const missing = required.filter((k) => !entry.config[k]);
          if (missing.length > 0) {
            console.warn(`ChatManager: skipping "${key}" — missing required credentials: ${missing.join(", ")}`);
            continue;
          }
        }

        try {
          if (entry.platform === "slack") {
            // Socket Mode (appToken present) connects an OUTBOUND WebSocket to
            // Slack — like Discord/Mattermost — so it needs no public inbound
            // URL and survives host restarts without re-pointing a tunnel.
            // Falls back to webhook/Events-API mode (signingSecret) otherwise.
            // The socket is started automatically by the adapter's
            // initialize(chat), which runs during newBot.initialize() below.
            const useSocketMode = slackMode === "socket";
            const slackAdapter = useSocketMode
              ? createSlackAdapter({
                  botToken: entry.config.botToken,
                  appToken: entry.config.appToken,
                  mode: "socket",
                })
              : createSlackAdapter({
                  botToken: entry.config.botToken,
                  signingSecret: entry.config.signingSecret,
                });
            console.log(
              `ChatManager: Slack adapter "${key}" using ${useSocketMode ? "socket" : "webhook"} mode`,
            );
            adapterInstances[key] = slackAdapter;
            // Cache team_id → connectionId for URL-based file routing
            try {
              // @chat-adapter/slack@4.28.x resolves the bot token per request
              // (defaultBotTokenProvider) rather than authenticating the raw
              // client, so auth.test() must be given the token explicitly or it
              // fails with not_authed and Slack file routing is degraded.
              const authResult = await (slackAdapter as any).client.auth.test({ token: entry.config.botToken });
              if (authResult?.team_id) {
                this.workspaceIdMap.set(authResult.team_id, entry.connectionId);
                console.log(`ChatManager: cached Slack team_id=${authResult.team_id} → connection=${entry.connectionId}`);
              }
              // auth.test also returns `url` (e.g. "https://beever.slack.com/");
              // its subdomain is the workspace domain needed to build clickable
              // message permalinks for citations. Best-effort — a missing/odd
              // url just means citations stay unlinked, never an error.
              const workspaceDomain = parseSlackWorkspaceDomain(authResult?.url);
              if (workspaceDomain) {
                this.workspaceDomainMap.set(entry.connectionId, workspaceDomain);
                console.log(`ChatManager: cached Slack workspace_domain=${workspaceDomain} → connection=${entry.connectionId}`);
              }
            } catch (err) {
              console.warn(`ChatManager: auth.test failed for "${key}", file routing may be degraded:`, safeErrorMessage(err));
            }
          } else if (entry.platform === "discord") {
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
            adapterInstances[key] = createTeamsAdapter({
              appId: entry.config.appId,
              appPassword: entry.config.appPassword,
              appTenantId: entry.config.appTenantId,
              appType: entry.config.appType as "MultiTenant" | "SingleTenant" | undefined,
            });
          } else if (entry.platform === "telegram") {
            adapterInstances[key] = createTelegramAdapter({
              botToken: entry.config.botToken,
              secretToken: entry.config.secretToken,
            });
          } else if (entry.platform === "mattermost") {
            adapterInstances[key] = createMattermostAdapter({
              baseUrl: entry.config.baseUrl,
              botToken: entry.config.botToken,
            });
          } else {
            console.warn(`ChatManager: unknown platform "${entry.platform}", skipping`);
          }
        } catch (err) {
          console.error(`ChatManager: failed to create adapter for "${key}":`, safeErrorMessage(err));
        }
      }

      if (Object.keys(adapterInstances).length === 0) {
        console.warn("ChatManager: no valid adapters could be created");
        return;
      }

      const newBot = new Chat({
        userName: "beever",
        adapters: adapterInstances as Record<string, import("chat").Adapter>,
        state: createRedisState({ url: this.redisUrl }),
      });

      this.registerHandlers(newBot);
      this.currentBot = newBot;

      // Eagerly initialize the Chat instance so every adapter gets
      // `initialize(chat)` called (which sets `adapter.chat` and connects the
      // Redis state). The Chat SDK otherwise defers this until the first
      // inbound webhook, which left bridge-driven reads (e.g. Teams Graph
      // channel-message fetch via `getGraphContext` → Redis cache) unable to
      // resolve context until the bot had been @mentioned. Initializing here
      // makes history fetch work without an @mention, like the other bridges.
      // Best-effort: a failure must not abort the rebuild for other platforms.
      try {
        await newBot.initialize();
      } catch (err) {
        console.warn("ChatManager: Chat.initialize() failed (adapters still registered):", safeErrorMessage(err));
      }


      console.log(`ChatManager: bot rebuilt with adapters: ${Object.keys(adapterInstances).join(", ")}`);
    } finally {
      this.transitioning = false;
      this.notifyRebuildCompleteListeners();
    }
  }

  /**
   * RES-286 — schedule periodic adapter recycle to drop accumulated state in
   * long-lived adapter websockets (the chat-adapter-mattermost leak in
   * particular). Each tick calls `rebuild()` which discards the old Chat
   * instance + every adapter and reconstructs them fresh from stored configs.
   *
   * Re-entry is naturally guarded by `transitioning`: if the previous rebuild
   * is still in flight when the timer fires, the new rebuild simply runs
   * after; `rebuild()` is idempotent.
   *
   * Webhook deliveries during the ~1 s rebuild window are buffered by
   * `WebhookBuffer` and replayed once the new bot is ready, so users see
   * no degradation from a recycle.
   *
   * Calling this more than once replaces the existing timer.
   *
   * @param intervalMs how often to recycle. Pass 0 or a negative value to
   *                   disable (useful for tests and local dev).
   */
  scheduleAdapterRecycle(intervalMs: number): void {
    if (this.recycleTimer) {
      clearInterval(this.recycleTimer);
      this.recycleTimer = null;
    }
    if (intervalMs <= 0) {
      console.log("ChatManager: adapter recycle disabled");
      return;
    }
    this.consecutiveRecycleFailures = 0;
    this.recycleTimer = setInterval(() => {
      if (this.adapters.size === 0) return;
      if (this.transitioning) return;
      console.log(`ChatManager: scheduled adapter recycle (every ${Math.round(intervalMs / 1000)}s)`);
      this.rebuild()
        .then(() => {
          this.consecutiveRecycleFailures = 0;
        })
        .catch((err: unknown) => {
          this.consecutiveRecycleFailures++;
          console.error(
            `ChatManager: scheduled recycle failed (${this.consecutiveRecycleFailures}/${ChatManager.RECYCLE_FAILURE_LIMIT}):`,
            safeErrorMessage(err),
          );
          if (this.consecutiveRecycleFailures >= ChatManager.RECYCLE_FAILURE_LIMIT) {
            console.error(
              `ChatManager: recycle halted after ${this.consecutiveRecycleFailures} consecutive failures; ` +
                "investigate logs and re-enable via a process restart or another scheduleAdapterRecycle() call",
            );
            this.stopAdapterRecycle();
          }
        });
    }, intervalMs);
    this.recycleTimer.unref();
    console.log(`ChatManager: adapter recycle enabled (every ${Math.round(intervalMs / 1000)}s)`);
  }

  /** Stop the recycle timer (used during graceful shutdown / tests). */
  stopAdapterRecycle(): void {
    if (this.recycleTimer) {
      clearInterval(this.recycleTimer);
      this.recycleTimer = null;
    }
    this.consecutiveRecycleFailures = 0;
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
  getAdapterConfig(connectionId: string): Record<string, string> | null {
    for (const [, entry] of this.adapters.entries()) {
      if (entry.connectionId === connectionId) return entry.config;
    }
    return null;
  }

  getConnectionInfo(connectionId: string): { platform: string; connectionId: string; config: Record<string, string> } | null {
    for (const [, entry] of this.adapters.entries()) {
      if (entry.connectionId === connectionId) {
        return { platform: entry.platform, connectionId: entry.connectionId, config: entry.config };
      }
    }
    return null;
  }

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

  /**
   * Returns the number of registered adapters.
   */
  adapterCount(): number {
    return this.adapters.size;
  }

  /**
   * Returns a stable fingerprint of the current adapter set (for change detection).
   * The fingerprint is a sorted, joined string of composite keys.
   */
  adapterFingerprint(): string {
    return [...this.adapters.keys()].sort().join(",");
  }

  /**
   * Resolve a workspace/team identifier (e.g. Slack team_id) to a connectionId.
   * Returns null if no mapping is found.
   */
  getConnectionForWorkspaceId(workspaceId: string): string | null {
    return this.workspaceIdMap.get(workspaceId) ?? null;
  }

  /** Slack workspace domain for a specific connection, or null if unknown. */
  getWorkspaceDomain(connectionId: string): string | null {
    return this.workspaceDomainMap.get(connectionId) ?? null;
  }

  /**
   * Slack workspace domain for the FIRST adapter of `platform` — mirrors how
   * `getAdapter(platform)` selects an adapter, so a channel resolved via the
   * platform-level bridge gets a consistent domain. Returns null when unknown
   * (non-Slack, auth.test failed, or multi-workspace where the first adapter's
   * domain doesn't apply — callers degrade to an unlinked citation).
   */
  getWorkspaceDomainForPlatform(platform: string): string | null {
    for (const { platform: p, connectionId } of this.listAdapters()) {
      if (p === platform) {
        const domain = this.workspaceDomainMap.get(connectionId);
        if (domain) return domain;
      }
    }
    return null;
  }
}

/**
 * Decide how a Slack adapter should be built from its credentials.
 *
 * Slack supports two inbound transports:
 *  - "socket": Socket Mode — needs `botToken` + `appToken` (xapp-...). The
 *    adapter opens an OUTBOUND WebSocket to Slack (like Discord/Mattermost), so
 *    no public inbound URL/tunnel is required and it survives host restarts.
 *  - "webhook": Events API — needs `botToken` + `signingSecret`. Slack POSTs
 *    events to a public inbound URL, so it requires a tunnel in local dev.
 *
 * When both `appToken` and `signingSecret` are present, socket mode wins (it is
 * strictly easier to operate locally). Returns `ok:false` with the missing keys
 * when neither transport can be satisfied.
 */
export function planSlackAdapter(config: Record<string, unknown>):
  | { ok: true; mode: "socket" | "webhook" }
  | { ok: false; missing: string[] } {
  const has = (k: string) => Boolean(config[k]);
  const missing: string[] = [];
  if (!has("botToken")) missing.push("botToken");
  if (!has("signingSecret") && !has("appToken")) missing.push("signingSecret|appToken");
  if (missing.length > 0) return { ok: false, missing };
  return { ok: true, mode: has("appToken") ? "socket" : "webhook" };
}

/**
 * Parse the Slack workspace domain (the *.slack.com subdomain, e.g. "beever")
 * from an auth.test `url` like "https://beever.slack.com/". Returns null on a
 * missing/odd value rather than throwing — a missing domain just leaves
 * citations unlinked.
 */
export function parseSlackWorkspaceDomain(url: unknown): string | null {
  if (typeof url !== "string" || url.length === 0) return null;
  try {
    const host = new URL(url).hostname; // e.g. "beever.slack.com"
    if (!host.endsWith(".slack.com")) return null;
    const sub = host.slice(0, host.length - ".slack.com".length);
    // Reject empty / multi-label subdomains we can't turn into a permalink host.
    return /^[a-z0-9-]+$/i.test(sub) ? sub : null;
  } catch {
    return null;
  }
}
