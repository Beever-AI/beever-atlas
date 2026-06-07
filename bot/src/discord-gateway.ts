/**
 * Discord Gateway keep-alive supervisor.
 *
 * Why this exists
 * ---------------
 * The platforms differ in how inbound messages reach the bot:
 *   - Slack      → Events-API HTTP webhooks (no persistent connection needed)
 *   - Mattermost → the adapter opens & holds its own websocket
 *   - Discord    → the official @chat-adapter/discord adapter is
 *                  interaction/webhook based. Plain channel messages — including
 *                  ordinary @mentions — are Gateway `MESSAGE_CREATE` events that
 *                  arrive ONLY while a Gateway WebSocket is held open. The
 *                  adapter exposes `startGatewayListener(...)`, designed (per
 *                  https://chat-sdk.dev/adapters/official/discord) for a
 *                  serverless cron that re-invokes it every few minutes.
 *
 * Without something calling `startGatewayListener`, Discord delivers nothing to
 * the bot and @mentions get no reply. This module is the long-running-Node
 * equivalent of that cron: for each registered Discord connection it runs a
 * chain-on-completion loop that keeps exactly ONE Gateway listener alive at a
 * time (no overlap → no duplicate delivery → no double replies).
 *
 * In-process dispatch (no webhookUrl)
 * -----------------------------------
 * `startGatewayListener` is called WITHOUT a webhookUrl, which selects the
 * adapter's in-process gateway handler. That path dispatches messages straight
 * to the adapter's own `Chat` instance using the full discord.js `Message`
 * object — so it correctly detects thread messages (`message.channel.isThread()`
 * → continue in the thread) and both user and role mentions. The alternative
 * webhook-forwarding mode POSTs the *raw* gateway payload, which omits
 * `channel_type`; that loses thread context and breaks in-thread continuation,
 * so we deliberately avoid it.
 *
 * Scope guarantee
 * ---------------
 * Strictly Discord-scoped. `sync()` only ever touches adapters returned by
 * `getAdaptersByPlatform("discord")`, so when no Discord connection is
 * registered it is a complete no-op and Slack/Mattermost/Teams/Telegram are
 * entirely unaffected.
 */
import type { ChatManager } from "./chat-manager.js";

const DISCORD_API = "https://discord.com/api/v10";
/** Per-request timeout for the best-effort role-resolution REST calls. */
const ROLE_RESOLVE_TIMEOUT_MS = 8_000;
/** Cap on guilds scanned for the managed role, so a bot in very many servers
 *  can't turn resolution into hundreds of sequential calls. Discord returns at
 *  most 200 guilds per page and we don't paginate — a bot beyond this is far
 *  past this product's scope; we log and stop. */
const MAX_GUILDS_SCANNED = 200;

/** Structural subset of the Discord adapter this supervisor depends on. */
export interface DiscordGatewayAdapter {
  /** Role IDs treated as bot mentions (public field on the official adapter). */
  mentionRoleIds?: string[];
  startGatewayListener(
    options: { waitUntil: (task: Promise<unknown>) => void },
    durationMs: number,
    abortSignal: AbortSignal,
    /** Omitted → in-process dispatch (correct thread/mention handling). */
    webhookUrl?: string,
  ): Promise<{ status: number }>;
}

export interface DiscordGatewayOptions {
  /** Listener window per arming; re-armed immediately on completion. Default 9 min. */
  windowMs?: number;
  /** Minimum spacing between re-arms — guards against a hot loop if a window
   *  ends abnormally fast. Default 5 s. */
  retryMs?: number;
  /** Master switch. Defaults to enabled unless DISCORD_GATEWAY_DISABLED=1. */
  enabled?: boolean;
}

/** Abortable sleep — resolves early (without rejecting) when `signal` aborts. */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    // `onAbort` closes over `timer`; it only runs on a later abort event, by
    // which point `timer` is assigned — so `const` (no TDZ at call time).
    const onAbort = () => {
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      // Normal expiry: drop the abort listener so it doesn't linger on a
      // long-lived signal.
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Best-effort discovery of the bot's *managed* integration role(s).
 *
 * When a bot is added to a Discord server it gets an auto-created "managed"
 * role with the bot's name. Discord's autocomplete frequently inserts a ping of
 * that ROLE (`<@&roleId>`) rather than the bot USER (`<@userId>`) when a member
 * types "@BotName" — and the adapter only treats role pings as mentions when
 * the role is in `mentionRoleIds`. Returning the managed role here lets the
 * natural "@BotName" work without per-server configuration.
 *
 * Talks only to discord.com with the bot token; no caller-controlled host, so
 * there is no SSRF surface. Returns [] on any failure (the bot still answers
 * direct user mentions).
 */
export async function resolveManagedRoleIds(
  botToken: string,
  applicationId: string,
): Promise<string[]> {
  const headers = { Authorization: `Bot ${botToken}` };
  const roleIds = new Set<string>();
  try {
    const guildsRes = await fetch(`${DISCORD_API}/users/@me/guilds`, {
      headers,
      signal: AbortSignal.timeout(ROLE_RESOLVE_TIMEOUT_MS),
    });
    if (!guildsRes.ok) return [];
    const allGuilds = (await guildsRes.json()) as Array<{ id?: string }>;
    if (allGuilds.length > MAX_GUILDS_SCANNED) {
      console.warn(
        `Discord gateway: bot is in ${allGuilds.length} guilds; scanning only the first ${MAX_GUILDS_SCANNED} for the managed role`,
      );
    }
    const guilds = allGuilds.slice(0, MAX_GUILDS_SCANNED);
    for (const guild of guilds) {
      if (!guild?.id) continue;
      try {
        const rolesRes = await fetch(
          `${DISCORD_API}/guilds/${encodeURIComponent(guild.id)}/roles`,
          { headers, signal: AbortSignal.timeout(ROLE_RESOLVE_TIMEOUT_MS) },
        );
        if (!rolesRes.ok) continue;
        const roles = (await rolesRes.json()) as Array<{
          id: string;
          managed?: boolean;
          tags?: { bot_id?: string };
        }>;
        for (const role of roles) {
          if (role.managed && role.tags?.bot_id === applicationId) {
            roleIds.add(role.id);
          }
        }
      } catch {
        /* per-guild best-effort — skip this guild */
      }
    }
  } catch {
    /* best-effort — bot still answers direct user mentions */
  }
  return [...roleIds];
}

export class DiscordGatewaySupervisor {
  /** Monotonic token; every loop checks it so a `sync()`/`stop()` retires stale loops. */
  private generation = 0;
  /** connectionId → controller for the loop currently bound to that connection. */
  private readonly controllers = new Map<string, AbortController>();
  /** connectionId → resolved managed role ids. Managed roles effectively never
   *  change for the life of the process, so this avoids re-hitting Discord on
   *  every rebuild/recycle and makes re-syncs instant (no startup race). Only
   *  non-empty results are cached, so a transient failure is retried later. */
  private readonly managedRoleCache = new Map<string, string[]>();
  private readonly windowMs: number;
  private readonly retryMs: number;
  private readonly enabled: boolean;

  constructor(
    private readonly chatManager: ChatManager,
    opts: DiscordGatewayOptions = {},
  ) {
    this.windowMs = opts.windowMs ?? 540_000; // 9 minutes
    this.retryMs = opts.retryMs ?? 5_000;
    this.enabled = opts.enabled ?? process.env.DISCORD_GATEWAY_DISABLED !== "1";
  }

  /**
   * Reconcile Gateway listeners with the current Discord adapter set. Safe to
   * call repeatedly — at startup and after every adapter rebuild/recycle. Each
   * call retires loops bound to now-stale adapter instances and starts exactly
   * one fresh loop per current Discord connection. No-op when no Discord
   * adapter is registered.
   */
  sync(): void {
    if (!this.enabled) return;

    // Invalidate every in-flight loop: a rebuild replaces adapter instances, so
    // the old loops are bound to dead adapters and must not keep forwarding.
    this.generation += 1;
    const gen = this.generation;
    for (const controller of this.controllers.values()) controller.abort();
    this.controllers.clear();

    for (const { connectionId, adapter } of this.chatManager.getAdaptersByPlatform("discord")) {
      const controller = new AbortController();
      this.controllers.set(connectionId, controller);
      const typed = adapter as DiscordGatewayAdapter;
      // Resolve mention roles FIRST, then start the listener, so the very first
      // "@BotName" role-ping isn't missed. `applyMentionRoles` is cached and
      // time-bounded (8s), so it can't delay the gateway materially; if it
      // somehow fails or hangs, the loop still starts and direct user mentions
      // keep working.
      void this.applyMentionRoles(typed, connectionId).finally(() => {
        if (controller.signal.aborted || gen !== this.generation) return;
        void this.runLoop(connectionId, typed, controller.signal, gen);
      });
    }
  }

  /** Abort all listeners — call on graceful shutdown. */
  stop(): void {
    this.generation += 1;
    for (const controller of this.controllers.values()) controller.abort();
    this.controllers.clear();
  }

  /** Number of connections currently supervised (for tests/diagnostics). */
  activeCount(): number {
    return this.controllers.size;
  }

  private async applyMentionRoles(
    adapter: DiscordGatewayAdapter,
    connectionId: string,
  ): Promise<void> {
    try {
      const cfg = this.chatManager.getAdapterConfig(connectionId);
      const botToken = cfg?.botToken;
      const applicationId = cfg?.applicationId;
      if (!botToken || !applicationId) return;
      let managed = this.managedRoleCache.get(connectionId);
      if (managed === undefined) {
        managed = await resolveManagedRoleIds(botToken, applicationId);
        // Cache only a successful (non-empty) result so a transient REST
        // failure is retried on the next sync rather than memoised as "none".
        if (managed.length > 0) this.managedRoleCache.set(connectionId, managed);
      }
      if (managed.length === 0) return;
      // Merge with any env/config-derived ids the adapter already holds.
      const merged = Array.from(new Set([...(adapter.mentionRoleIds ?? []), ...managed]));
      adapter.mentionRoleIds = merged;
      console.log(
        `Discord gateway: connection ${connectionId} treating managed role(s) [${managed.join(", ")}] as bot mentions`,
      );
    } catch (err) {
      console.warn(
        `Discord gateway: managed-role resolution failed for ${connectionId}:`,
        err instanceof Error ? err.message : String(err),
      );
    }
  }

  private async runLoop(
    connectionId: string,
    adapter: DiscordGatewayAdapter,
    signal: AbortSignal,
    gen: number,
  ): Promise<void> {
    console.log(`Discord gateway: starting keep-alive for connection ${connectionId}`);
    while (!signal.aborted && gen === this.generation) {
      const started = Date.now();
      try {
        let listenerPromise: Promise<unknown> = Promise.resolve();
        // No webhookUrl → in-process dispatch via the adapter's own Chat
        // instance, with correct thread + mention handling.
        const res = await adapter.startGatewayListener(
          { waitUntil: (task) => { listenerPromise = task; } },
          this.windowMs,
          signal,
        );
        if (res.status >= 400) {
          throw new Error(`startGatewayListener returned HTTP ${res.status}`);
        }
        // Resolves when the window elapses or `signal` aborts. No overlap with
        // the next arming → a given message is delivered exactly once.
        await listenerPromise;
      } catch (err) {
        if (signal.aborted || gen !== this.generation) break;
        console.warn(
          `Discord gateway: listener error for ${connectionId}:`,
          err instanceof Error ? err.message : String(err),
        );
      }
      if (signal.aborted || gen !== this.generation) break;
      // Pace re-arming: in steady state a window lasts windowMs so this is a
      // no-op, but if a window ends abnormally fast (e.g. an adapter that never
      // calls waitUntil, or a transient login failure) this prevents a hot loop.
      const elapsed = Date.now() - started;
      if (elapsed < this.retryMs) await sleep(this.retryMs - elapsed, signal);
    }
    console.log(`Discord gateway: keep-alive stopped for connection ${connectionId}`);
  }
}
