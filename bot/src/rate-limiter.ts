/**
 * In-memory sliding-window rate limiter.
 *
 * Gates inbound questions so a burst from one user can't hammer the backend.
 * Keyed per (platform, channel, user) — including the user id so one abuser
 * can't exhaust a shared channel's budget for everyone. Bounded + clock-
 * injectable for tests; no external dependency.
 */
export interface RateDecision {
  allowed: boolean;
  /** Milliseconds until the next request would be allowed (0 when allowed). */
  retryAfterMs: number;
}

export class RateLimiter {
  private readonly limit: number;
  private readonly windowMs: number;
  private readonly now: () => number;
  private readonly maxKeys: number;
  private readonly hits = new Map<string, number[]>();

  constructor(limit: number, windowMs: number, now: () => number = Date.now, maxKeys = 10_000) {
    this.limit = Math.max(1, limit);
    this.windowMs = Math.max(1, windowMs);
    this.now = now;
    this.maxKeys = Math.max(1, maxKeys);
  }

  /** Record an attempt and decide whether it's allowed under the window. */
  check(key: string): RateDecision {
    const t = this.now();
    const cutoff = t - this.windowMs;
    const recent = (this.hits.get(key) ?? []).filter((ts) => ts > cutoff);

    if (recent.length >= this.limit) {
      this.hits.set(key, recent);
      return { allowed: false, retryAfterMs: Math.max(0, recent[0] + this.windowMs - t) };
    }

    recent.push(t);
    this.hits.set(key, recent);
    if (this.hits.size > this.maxKeys) this.evict(cutoff);
    return { allowed: true, retryAfterMs: 0 };
  }

  /** Bound memory: drop empty/expired key buckets, then oldest, until within cap. */
  private evict(cutoff: number): void {
    for (const [key, arr] of this.hits) {
      const live = arr.filter((ts) => ts > cutoff);
      if (live.length === 0) this.hits.delete(key);
      else this.hits.set(key, live);
    }
    while (this.hits.size > this.maxKeys) {
      const oldest = this.hits.keys().next().value;
      if (oldest === undefined) break;
      this.hits.delete(oldest);
    }
  }
}
