import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { SharedAskPage } from "@/pages/SharedAskPage";

function installFetch(
  handler: (url: string, init?: RequestInit) => Response | Promise<Response>,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : (input as URL).toString();
      return handler(url, init);
    }),
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/ask/shared/:token" element={<SharedAskPage />} />
        <Route path="/ask" element={<div data-testid="ask-page">ask</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const OK_BODY = {
  title: "My Chat",
  created_at: "2026-04-10T12:00:00Z",
  visibility: "public",
  owner_user_id: "user-123",
  messages: [
    { role: "user", content: "hello", created_at: "2026-04-10T12:00:00Z" },
    { role: "assistant", content: "hi there", created_at: "2026-04-10T12:00:01Z" },
  ],
};

beforeEach(() => {
  // clean metas from prior tests
  document.head
    .querySelectorAll('meta[name="robots"], meta[name="referrer"]')
    .forEach((el) => el.remove());
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.head
    .querySelectorAll('meta[name="robots"], meta[name="referrer"]')
    .forEach((el) => el.remove());
});

describe("SharedAskPage", () => {
  it("renders messages when fetch succeeds", async () => {
    installFetch(async () => jsonResponse(OK_BODY));
    renderAt("/ask/shared/tok1");
    await waitFor(() =>
      expect(screen.getByTestId("shared-ask-page")).toBeInTheDocument(),
    );
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByText("hi there")).toBeInTheDocument();
    expect(screen.getByText(/Shared conversation — read only/i)).toBeInTheDocument();
  });

  it("hides composer and tool panels (read-only view)", async () => {
    installFetch(async () => jsonResponse(OK_BODY));
    renderAt("/ask/shared/tok1");
    await waitFor(() =>
      expect(screen.getByTestId("shared-ask-page")).toBeInTheDocument(),
    );
    // No composer / input bar / send button exists.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByText(/Send/i)).not.toBeInTheDocument();
  });

  it("renders 'Link revoked or not found' panel on 404", async () => {
    installFetch(async () => jsonResponse({ detail: "nope" }, 404));
    renderAt("/ask/shared/bad");
    await waitFor(() =>
      expect(screen.getByTestId("shared-not-found")).toBeInTheDocument(),
    );
    expect(screen.getByText(/Link revoked or not found/i)).toBeInTheDocument();
    expect(screen.getByText(/Back to Ask/i)).toBeInTheDocument();
  });

  it("shows owner chrome when owner_user_id matches beever_user_id in localStorage", async () => {
    localStorage.setItem("beever_user_id", "user-123");
    installFetch(async () => jsonResponse(OK_BODY));
    renderAt("/ask/shared/tok1");
    await waitFor(() =>
      expect(screen.getByTestId("shared-owner-chrome")).toBeInTheDocument(),
    );
    expect(screen.getByText(/You're the owner/i)).toBeInTheDocument();
  });

  it("does NOT show owner chrome when user id does not match", async () => {
    localStorage.setItem("beever_user_id", "someone-else");
    installFetch(async () => jsonResponse(OK_BODY));
    renderAt("/ask/shared/tok1");
    await waitFor(() =>
      expect(screen.getByTestId("shared-ask-page")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("shared-owner-chrome")).not.toBeInTheDocument();
  });

  it("injects <meta robots> and <meta referrer> into document.head", async () => {
    installFetch(async () => jsonResponse(OK_BODY));
    renderAt("/ask/shared/tok1");
    await waitFor(() =>
      expect(screen.getByTestId("shared-ask-page")).toBeInTheDocument(),
    );
    const robots = document.head.querySelector('meta[name="robots"]');
    const referrer = document.head.querySelector('meta[name="referrer"]');
    expect(robots?.getAttribute("content")).toBe("noindex, nofollow");
    expect(referrer?.getAttribute("content")).toBe("no-referrer");
  });

  it("retries with auth when initial fetch returns 401", async () => {
    let calls = 0;
    installFetch(async (url, init) => {
      calls += 1;
      if (calls === 1) return jsonResponse({ detail: "auth" }, 401);
      // Second call should be via authFetch (we don't assert Authorization
      // header here because authFetch only injects it if VITE_BEEVER_API_KEY
      // is set in test env — simply verify a second fetch occurred).
      void init;
      void url;
      return jsonResponse(OK_BODY);
    });
    renderAt("/ask/shared/tok1");
    await waitFor(() =>
      expect(screen.getByTestId("shared-ask-page")).toBeInTheDocument(),
    );
    expect(calls).toBeGreaterThanOrEqual(2);
  });
});
