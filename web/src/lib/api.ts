const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";
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

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
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
    throw new ApiError(
      response.status,
      error.code || "UNKNOWN",
      error.message || body?.detail || response.statusText,
    );
  }

  // 204 No Content has no body — return undefined instead of parsing JSON
  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "PUT",
      body: body ? JSON.stringify(body) : undefined,
    }),
  delete: <T>(path: string) =>
    request<T>(path, {
      method: "DELETE",
    }),
};

export { ApiError };

/**
 * Build a URL for browser-native loaders (<img src>, <a href>) that cannot
 * carry custom Authorization headers. Appends `?access_token=<key>` so
 * `require_user` can validate the request via query string.
 *
 * Use for /api/files/proxy and other endpoints consumed by <img>/<a>.
 * DO NOT use for programmatic fetch() calls — use authFetch / api.get there.
 */
export function buildLoaderUrl(path: string): string {
  const base = path.startsWith("http") ? path : `${API_BASE}${path}`;
  const apiKey = currentApiKey();
  if (!apiKey) return base;
  const sep = base.includes("?") ? "&" : "?";
  return `${base}${sep}access_token=${encodeURIComponent(apiKey)}`;
}
