import { useEffect, useState, useCallback } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import { authFetch, API_BASE } from "@/lib/api";

interface SharedMessage {
  role: "user" | "assistant";
  content: string;
  created_at?: string;
}

interface SharedResponse {
  title: string;
  messages: SharedMessage[];
  created_at: string;
  visibility: "owner" | "auth" | "public";
  owner_user_id: string;
}

type FetchState =
  | { status: "loading" }
  | { status: "ok"; data: SharedResponse }
  | { status: "not_found" }
  | { status: "error"; message: string };

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

function getCurrentUserId(): string | null {
  try {
    return localStorage.getItem("beever_user_id");
  } catch {
    return null;
  }
}

/**
 * Injects <meta name="referrer" content="no-referrer"> and
 * <meta name="robots" content="noindex, nofollow"> into document.head
 * for the lifetime of the mount. Cleans up on unmount.
 */
function useNoIndexNoReferrer(): void {
  useEffect(() => {
    const meta1 = document.createElement("meta");
    meta1.name = "referrer";
    meta1.content = "no-referrer";
    const meta2 = document.createElement("meta");
    meta2.name = "robots";
    meta2.content = "noindex, nofollow";
    document.head.appendChild(meta1);
    document.head.appendChild(meta2);
    return () => {
      meta1.parentNode?.removeChild(meta1);
      meta2.parentNode?.removeChild(meta2);
    };
  }, []);
}

export function SharedAskPage() {
  const { token } = useParams<{ token: string }>();
  const navigate = useNavigate();
  const [state, setState] = useState<FetchState>({ status: "loading" });

  useNoIndexNoReferrer();

  useEffect(() => {
    if (!token) {
      setState({ status: "not_found" });
      return;
    }
    let cancelled = false;
    (async () => {
      const url = `${API_BASE}/api/ask/shared/${token}`;
      try {
        // Try unauth first (public tier).
        let res = await fetch(url);
        if (res.status === 401) {
          // Retry with auth for owner/auth tiers.
          res = await authFetch(url);
        }
        if (cancelled) return;
        if (res.status === 404) {
          setState({ status: "not_found" });
          return;
        }
        if (!res.ok) {
          setState({ status: "error", message: `HTTP ${res.status}` });
          return;
        }
        const data = (await res.json()) as SharedResponse;
        if (cancelled) return;
        setState({ status: "ok", data });
      } catch (e) {
        if (cancelled) return;
        setState({
          status: "error",
          message: e instanceof Error ? e.message : "unknown error",
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const handleReSnapshot = useCallback(async () => {
    if (state.status !== "ok") return;
    // Owner-only: re-snapshot the source session using the owner endpoint.
    // We don't have source_session_id in the response; owners should use the
    // [Open in Ask] path then Share dialog to re-snapshot. This button is a
    // shortcut hint only when owner chrome is visible. No-op here for safety.
  }, [state]);

  if (state.status === "loading") {
    return (
      <div className="h-full flex items-center justify-center text-muted-foreground/60 text-sm">
        Loading shared conversation…
      </div>
    );
  }

  if (state.status === "not_found") {
    return (
      <div
        className="h-full flex items-center justify-center p-8"
        data-testid="shared-not-found"
      >
        <div className="max-w-md text-center flex flex-col items-center gap-4">
          <h1 className="font-heading text-xl text-foreground">
            Link revoked or not found
          </h1>
          <p className="text-sm text-muted-foreground">
            This shared conversation is no longer available.
          </p>
          <Link
            to="/ask"
            className="inline-flex items-center justify-center h-10 px-5 rounded-full text-sm font-medium border border-border bg-card hover:bg-muted transition-colors text-foreground"
          >
            Back to Ask
          </Link>
        </div>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="h-full flex items-center justify-center p-8">
        <div className="max-w-md text-center">
          <h1 className="font-heading text-xl text-foreground">
            Could not load conversation
          </h1>
          <p className="text-sm text-muted-foreground mt-2">{state.message}</p>
        </div>
      </div>
    );
  }

  const { data } = state;
  const currentUserId = getCurrentUserId();
  const isOwner = !!currentUserId && currentUserId === data.owner_user_id;

  return (
    <div
      className="h-full overflow-y-auto bg-background"
      data-testid="shared-ask-page"
    >
      <div className="max-w-3xl mx-auto w-full px-4 md:px-8 py-6 flex flex-col gap-6">
        <header className="flex flex-col gap-1 border-b border-border pb-4">
          <Link
            to="/ask"
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            Beever Atlas
          </Link>
          <h1 className="font-heading text-xl text-foreground">
            {data.title || "Shared conversation"}
          </h1>
          <div className="flex items-center justify-between flex-wrap gap-2">
            <p className="text-xs text-muted-foreground">
              Snapshot of {formatDate(data.created_at)}
            </p>
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground/70">
              Shared conversation — read only
            </span>
          </div>
        </header>

        {isOwner && (
          <div
            className="flex items-center gap-2 flex-wrap rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs"
            data-testid="shared-owner-chrome"
          >
            <span className="text-foreground">You're the owner —</span>
            <button
              onClick={() => navigate("/ask")}
              className="h-7 px-3 rounded-md border border-border bg-card hover:bg-muted"
            >
              Open in Ask
            </button>
            <button
              onClick={handleReSnapshot}
              className="h-7 px-3 rounded-md border border-border bg-card hover:bg-muted"
            >
              Re-snapshot
            </button>
            <button
              onClick={() => navigate("/ask")}
              className="h-7 px-3 rounded-md border border-red-500/40 text-red-500 hover:bg-red-500/10"
            >
              Revoke
            </button>
          </div>
        )}

        <ol
          className="flex flex-col gap-6"
          data-testid="shared-message-list"
        >
          {data.messages.map((m, i) => (
            <li
              key={i}
              className={
                m.role === "user"
                  ? "flex justify-end"
                  : "flex justify-start"
              }
            >
              <div
                className={
                  m.role === "user"
                    ? "max-w-[80%] bg-primary/10 rounded-2xl px-4 py-3 text-sm text-foreground whitespace-pre-wrap"
                    : "max-w-[90%] rounded-2xl px-4 py-3 text-sm text-foreground whitespace-pre-wrap"
                }
              >
                <div className="text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
                  {m.role}
                </div>
                {m.content}
              </div>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

export default SharedAskPage;
