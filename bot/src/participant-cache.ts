/**
 * Tiny TTL cache for a thread's human participant count.
 *
 * `getParticipants()` is called on every non-mention message in a subscribed
 * thread to decide whether the bot should stay quiet. On a busy thread that can
 * be a network round-trip per message; this caches the derived human count for
 * a short TTL so a burst of chatter doesn't hammer the platform API.
 *
 * Pure and clock-injectable so it's unit-testable without timers.
 */
export class ParticipantCache {
  private readonly ttlMs: number;
  private readonly now: () => number;
  private readonly maxEntries: number;
  private readonly entries = new Map<string, { count: number; expires: number }>();

  constructor(ttlMs: number, now: () => number = Date.now, maxEntries = 5000) {
    this.ttlMs = ttlMs;
    this.now = now;
    this.maxEntries = Math.max(1, maxEntries);
  }

  /** Cached human count for a thread, or undefined if absent/expired/disabled. */
  get(threadId: string): number | undefined {
    if (this.ttlMs <= 0) return undefined;
    const hit = this.entries.get(threadId);
    if (!hit) return undefined;
    if (hit.expires <= this.now()) {
      this.entries.delete(threadId);
      return undefined;
    }
    return hit.count;
  }

  set(threadId: string, count: number): void {
    if (this.ttlMs <= 0) return;
    this.entries.set(threadId, { count, expires: this.now() + this.ttlMs });
    if (this.entries.size > this.maxEntries) this.evict();
  }

  /** Bound memory: drop expired entries first, then oldest, until within cap. */
  private evict(): void {
    const now = this.now();
    for (const [key, value] of this.entries) {
      if (value.expires <= now) this.entries.delete(key);
    }
    while (this.entries.size > this.maxEntries) {
      const oldest = this.entries.keys().next().value;
      if (oldest === undefined) break;
      this.entries.delete(oldest);
    }
  }
}
