/**
 * Bot startup env validation (issue #53).
 *
 * Lightweight WARN-only checks; never gates startup. The bot is intentionally
 * tolerant of partial configuration (connections are loaded from the backend
 * at runtime, with .env as fallback), so a misconfigured env should produce
 * a loud log line at boot — not a hard fail mid-flight.
 *
 * Detection is split from logging so tests can assert on the warning list
 * without mocking console.warn.
 */

type WarnableEnv = {
  BACKEND_URL?: string | undefined;
  REDIS_URL?: string | undefined;
  BEEVER_API_KEYS?: string | undefined;
  BRIDGE_API_KEY?: string | undefined;
  BEEVER_ENV?: string | undefined;
  NODE_ENV?: string | undefined;
};

const URL_CHECKS: ReadonlyArray<readonly [keyof WarnableEnv, string, readonly string[]]> = [
  ["BACKEND_URL", "http://localhost:8000", ["http:", "https:"]],
  ["REDIS_URL", "redis://localhost:6379", ["redis:", "rediss:"]],
];

export function detectEnvWarnings(env: WarnableEnv = process.env): string[] {
  const warnings: string[] = [];

  for (const [name, fallback, allowed] of URL_CHECKS) {
    const value = env[name] || fallback;
    let parsed: URL;
    try {
      parsed = new URL(value);
    } catch {
      warnings.push(`${name}=${value} is not a valid URL`);
      continue;
    }
    if (!allowed.includes(parsed.protocol)) {
      warnings.push(
        `${name}=${value} has unexpected scheme '${parsed.protocol}' (expected ${allowed.join(" / ")})`,
      );
    }
  }

  const hasApiKey = (env.BEEVER_API_KEYS || "")
    .split(",")
    .map((k) => k.trim())
    .some(Boolean);
  if (!hasApiKey) {
    warnings.push(
      "BEEVER_API_KEYS is empty — bot cannot authenticate to backend; /api/channels/*/ask calls will return 401",
    );
  }

  // Bridge auth: only warn in production. Dev allows BRIDGE_ALLOW_UNAUTH=true
  // as an explicit opt-in escape hatch (see bridge.ts:checkAuth).
  const mode = env.BEEVER_ENV || env.NODE_ENV || "";
  if (mode === "production" && !env.BRIDGE_API_KEY) {
    warnings.push(
      "BRIDGE_API_KEY is empty in production — incoming backend webhooks will be rejected as 401",
    );
  }

  return warnings;
}

export function validateEnv(env: WarnableEnv = process.env): void {
  for (const w of detectEnvWarnings(env)) {
    console.warn(`[validateEnv] WARN: ${w}`);
  }
}
