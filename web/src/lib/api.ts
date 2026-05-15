export const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";
const REQUEST_TIMEOUT_MS = 15000;

// Read the key lazily on every call so HMR-stale module instances (which can
// capture an undefined value from before .env.local was written) cannot leak
// unauthenticated requests. Runtime cost is negligible; import.meta.env is a
// static object baked by Vite.
function currentApiKey(): string | undefined {
  const key = import.meta.env.VITE_BEEVER_API_KEY as string | undefined;
  return key && key.length > 0 ? key : undefined;
}

let _missingKeyWarned = false;
function warnIfMissingKey(key: string | undefined): void {
  if (!key && !_missingKeyWarned) {
    _missingKeyWarned = true;
    console.warn(
      "[api] VITE_BEEVER_API_KEY is not set — /api/* requests will omit the Authorization header. " +
        "Set it in web/.env.local to match a value in BEEVER_API_KEYS server-side.",
    );
  }
}

/**
 * Returns true when a URL targets a path that must not carry the
 * Authorization header (only /api/health is exempt server-side).
 */
function isAuthExempt(path: string): boolean {
  // Strip origin + query
  const pathname = path.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
  return pathname === "/api/health" || pathname.endsWith("/api/health");
}

function hasAuthHeader(headers: HeadersInit | undefined): boolean {
  if (!headers) return false;
  if (headers instanceof Headers) return headers.has("Authorization");
  if (Array.isArray(headers)) return headers.some(([k]) => k.toLowerCase() === "authorization");
  return Object.keys(headers).some((k) => k.toLowerCase() === "authorization");
}

/**
 * fetch() wrapper that injects `Authorization: Bearer <VITE_BEEVER_API_KEY>`
 * on every /api/* request unless the caller supplied an Authorization header
 * or the request targets /api/health (exempt server-side).
 *
 * Accepts the same arguments as the global fetch. Use this instead of raw
 * fetch() for any call into the backend API.
 */
export function authFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const apiKey = currentApiKey();
  warnIfMissingKey(apiKey);
  const urlStr = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
  const shouldInject = !!apiKey && !isAuthExempt(urlStr) && !hasAuthHeader(init?.headers);
  if (!shouldInject) {
    return fetch(input, init);
  }
  const merged = new Headers(init?.headers);
  merged.set("Authorization", `Bearer ${apiKey}`);
  return fetch(input, { ...init, headers: merged });
}

class ApiError extends Error {
  status: number;
  code: string;
  detail: unknown;

  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await authFetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...options?.headers,
      },
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(408, "TIMEOUT", "Request timed out");
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = body?.error || {};
    // FastAPI puts structured payloads under ``body.detail``. Preserve that
    // object on the thrown ApiError so callers can branch on detail.error
    // (e.g. dim_mismatch_requires_migration → open re-embed modal).
    const detail = body?.detail;
    let message = error.message || response.statusText;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail && typeof detail === "object" && typeof (detail as { message?: unknown }).message === "string") {
      message = (detail as { message: string }).message;
    }
    throw new ApiError(
      response.status,
      error.code || (detail && typeof detail === "object" && typeof (detail as { error?: unknown }).error === "string" ? (detail as { error: string }).error : "UNKNOWN"),
      message,
      detail,
    );
  }

  // 204 No Content has no body — return undefined instead of parsing JSON
  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string, options?: { headers?: Record<string, string> }) =>
    request<T>(path, { headers: options?.headers }),
  post: <T>(path: string, body?: unknown, options?: { headers?: Record<string, string> }) =>
    request<T>(path, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
      headers: options?.headers,
    }),
  put: <T>(path: string, body?: unknown, options?: { headers?: Record<string, string> }) =>
    request<T>(path, {
      method: "PUT",
      body: body ? JSON.stringify(body) : undefined,
      headers: options?.headers,
    }),
  patch: <T>(path: string, body?: unknown, options?: { headers?: Record<string, string> }) =>
    request<T>(path, {
      method: "PATCH",
      body: body ? JSON.stringify(body) : undefined,
      headers: options?.headers,
    }),
  delete: <T>(path: string, options?: { headers?: Record<string, string> }) =>
    request<T>(path, { method: "DELETE", headers: options?.headers }),
};

/**
 * Returns headers needed to call /api/dev/* endpoints. Reads
 * `VITE_BEEVER_ADMIN_TOKEN` from Vite env; if unset, returns an empty object
 * and the request will 401.
 */
export function adminHeaders(): Record<string, string> {
  const token = import.meta.env.VITE_BEEVER_ADMIN_TOKEN as string | undefined;
  return token ? { "X-Admin-Token": token } : {};
}

export { ApiError };

/**
 * Build a URL for browser-native loaders (<img src>, <a href>) that cannot
 * carry custom Authorization headers. Appends `?access_token=<key>` so
 * `require_user_loader` can validate the request via query string.
 *
 * Issue #89 — this is now the FALLBACK. Prefer `mintLoaderUrl` (async,
 * signed, scoped) for new code. `buildLoaderUrl` is kept for the
 * migration-window fallback inside `mintLoaderUrl` and will be removed
 * in the follow-up PR that flips `BEEVER_LOADER_RAW_KEY_FALLBACK=false`.
 */
export function buildLoaderUrl(path: string): string {
  const base = path.startsWith("http") ? path : `${API_BASE}${path}`;
  const apiKey = currentApiKey();
  if (!apiKey) return base;
  const sep = base.includes("?") ? "&" : "?";
  return `${base}${sep}access_token=${encodeURIComponent(apiKey)}`;
}

// ── Issue #89: HMAC-signed scoped loader tokens ─────────────────────────

/**
 * Strip query string + hash to extract the route path portion of a URL or
 * URI fragment. The mint endpoint binds tokens to route prefixes (e.g.
 * `/api/files/proxy`) — keying the cache by route path lets multiple
 * `<img>` URLs share a single token within the TTL window.
 */
function routePathOnly(pathOrUrl: string): string {
  const noHash = pathOrUrl.split("#", 1)[0] ?? pathOrUrl;
  const noQuery = noHash.split("?", 1)[0] ?? noHash;
  // If a full URL slipped in, strip the origin too.
  try {
    const u = new URL(noQuery, "http://placeholder.invalid");
    return u.pathname;
  } catch {
    return noQuery;
  }
}

interface CachedLoaderToken {
  token: string;
  expiresAt: number; // unix epoch seconds
}

const _loaderTokenCache = new Map<string, CachedLoaderToken>();

// 30s buffer before expiry — re-mint just before the token would refuse
// at the verifier so in-flight image loads aren't caught at the boundary.
const _CACHE_REFRESH_BUFFER_SECONDS = 30;

/** Test-only — clear the in-memory cache so vitest tests are isolated. */
export function _resetLoaderTokenCache(): void {
  _loaderTokenCache.clear();
}

/**
 * Mint (or look up cached) a signed loader token for the route portion of
 * `path`, then return `path` with `&loader_token=<token>` appended.
 *
 * Caching: tokens are cached per route path (e.g. `/api/files/proxy`).
 * Multiple `<img>` URLs targeting the same route share one token within
 * the TTL minus a 30-second buffer.
 *
 * Fallback: any error from the mint endpoint (404, 5xx, network failure)
 * falls through to the legacy `buildLoaderUrl` (raw API key) so image
 * rendering survives during the migration window. `<ProxiedImage>`'s
 * `onError` retry is bounded at 2 attempts to prevent loops.
 */
export async function mintLoaderUrl(
  path: string,
  opts?: { forceRefresh?: boolean },
): Promise<string> {
  const fullPath = path.startsWith("http") ? path : `${API_BASE}${path}`;
  const routePath = routePathOnly(path);
  const now = Math.floor(Date.now() / 1000);

  if (!opts?.forceRefresh) {
    const cached = _loaderTokenCache.get(routePath);
    if (cached && cached.expiresAt > now + _CACHE_REFRESH_BUFFER_SECONDS) {
      return _appendLoaderToken(fullPath, cached.token);
    }
  }

  try {
    const resp = await authFetch(`${API_BASE}/api/auth/loader-token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: routePath }),
    });
    if (!resp.ok) {
      throw new Error(`mintLoaderUrl: server returned ${resp.status}`);
    }
    const body = (await resp.json()) as { token: string; expires_at: number };
    if (!body.token || typeof body.expires_at !== "number") {
      throw new Error("mintLoaderUrl: malformed response");
    }
    _loaderTokenCache.set(routePath, {
      token: body.token,
      expiresAt: body.expires_at,
    });
    return _appendLoaderToken(fullPath, body.token);
  } catch (err) {
    // Migration-window fallback: the server might not have the new
    // endpoint, the secret might be unset, or the network might be flaky.
    // Falling back to `buildLoaderUrl` keeps images rendering as long as
    // `BEEVER_LOADER_RAW_KEY_FALLBACK=true` on the backend.
    console.warn("mintLoaderUrl: falling back to raw key", err);
    return buildLoaderUrl(path);
  }
}

function _appendLoaderToken(fullPath: string, token: string): string {
  const sep = fullPath.includes("?") ? "&" : "?";
  return `${fullPath}${sep}loader_token=${encodeURIComponent(token)}`;
}
