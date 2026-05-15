import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";

// The component reads ``import.meta.env.VITE_BEEVER_ADMIN_TOKEN`` at module
// load time. Stubbing it must happen BEFORE the component imports so the
// guard renders the real dashboard, not the access-denied fallback.
vi.stubEnv("VITE_BEEVER_ADMIN_TOKEN", "test-admin-token");

import { WikiDrift } from "../WikiDrift";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeFetchResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const NOW = Date.now();

const PASSING_FIXTURE = {
  channels: [
    {
      channel_id: "C1",
      page_count: 10,
      levenshtein_section_p50_median: 0.08,
      levenshtein_section_p95_median: 0.18,
      last_run_ts: new Date(NOW - 5 * 60_000).toISOString(),
      pass_criterion_met: true,
    },
    {
      channel_id: "C2",
      page_count: 6,
      levenshtein_section_p50_median: 0.10,
      levenshtein_section_p95_median: 0.22,
      last_run_ts: new Date(NOW - 12 * 60_000).toISOString(),
      pass_criterion_met: true,
    },
  ],
  pass: true,
  data_fresh: true,
};

const FAILING_FIXTURE = {
  channels: [
    {
      channel_id: "B",
      page_count: 8,
      levenshtein_section_p50_median: 0.22,
      levenshtein_section_p95_median: 0.35,
      last_run_ts: new Date(NOW - 5 * 60_000).toISOString(),
      pass_criterion_met: false,
    },
  ],
  pass: false,
  data_fresh: true,
};

const STALE_FIXTURE = {
  channels: [
    {
      channel_id: "C1",
      page_count: 4,
      levenshtein_section_p50_median: 0.10,
      levenshtein_section_p95_median: 0.20,
      last_run_ts: new Date(NOW - 90 * 60_000).toISOString(), // 90 min old
      pass_criterion_met: true,
    },
  ],
  pass: true,
  data_fresh: false,
};

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("WikiDrift dashboard", () => {
  it("renders the PASSING banner when fixture has pass=true", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse(PASSING_FIXTURE),
    );
    render(<WikiDrift />);
    await waitFor(() => {
      expect(screen.getByTestId("drift-banner-pass")).toBeInTheDocument();
    });
    expect(screen.getByTestId("drift-banner-pass")).toHaveTextContent(
      /PASSING/i,
    );
    expect(screen.getByTestId("drift-banner-pass")).toHaveTextContent(
      /2 channels/,
    );
  });

  it("renders the FAILING banner with worst drift + count when pass=false", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse(FAILING_FIXTURE),
    );
    render(<WikiDrift />);
    await waitFor(() => {
      expect(screen.getByTestId("drift-banner-fail")).toBeInTheDocument();
    });
    const banner = screen.getByTestId("drift-banner-fail");
    expect(banner).toHaveTextContent(/FAILING/i);
    expect(banner).toHaveTextContent(/0\.22/);
    expect(banner).toHaveTextContent(/1 channel\b/);
  });

  it("hides the pass/fail banner when channels list is empty", async () => {
    // Regression: with an empty drift_reports collection the API returns
    // ``{channels: [], pass: false, data_fresh: false}`` which the banner
    // would otherwise mis-render as a red "FAILING — drift 0.00 exceeds
    // threshold on 0 channels". The empty state should rely on the
    // ChannelTable's own "no reports yet" copy instead.
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse({ channels: [], pass: false, data_fresh: false }),
    );
    render(<WikiDrift />);
    await waitFor(() => {
      expect(screen.getByTestId("drift-empty")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("drift-banner-fail")).not.toBeInTheDocument();
    expect(screen.queryByTestId("drift-banner-pass")).not.toBeInTheDocument();
    expect(screen.queryByTestId("drift-banner-stale")).not.toBeInTheDocument();
  });

  it("renders the data_fresh warning when last report > 1h old", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse(STALE_FIXTURE),
    );
    render(<WikiDrift />);
    await waitFor(() => {
      expect(screen.getByTestId("drift-banner-stale")).toBeInTheDocument();
    });
    // Despite stale data, the pass banner remains green per the spec.
    expect(screen.getByTestId("drift-banner-pass")).toBeInTheDocument();
  });

  it("renders per-channel rows with relative time + threshold marker", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse(PASSING_FIXTURE),
    );
    render(<WikiDrift />);
    await waitFor(() => {
      expect(screen.getByTestId("drift-row-C1")).toBeInTheDocument();
    });
    expect(screen.getByTestId("drift-row-C1")).toHaveTextContent("C1");
    expect(screen.getByTestId("drift-row-C1")).toHaveTextContent("0.080");
    expect(screen.getByTestId("drift-row-C1")).toHaveTextContent("0.180");
    // Either '5m ago' or 'just now' depending on test timing margin.
    expect(screen.getByTestId("drift-row-C1")).toHaveTextContent(
      /(m ago|just now)/i,
    );
  });

  it("auto-refreshes after 5 minutes via setInterval", async () => {
    // Only fake the interval-related timers — waitFor's polling uses
    // setTimeout internally, so a blanket vi.useFakeTimers() deadlocks
    // the assertion loop. Pinning the fake-timers surface to the timers
    // the SUT actually uses keeps both sides cooperative.
    vi.useFakeTimers({
      toFake: ["setInterval", "clearInterval"],
    });
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValueOnce(makeFetchResponse(FAILING_FIXTURE));

    render(<WikiDrift />);
    await waitFor(() => {
      expect(screen.getByTestId("drift-banner-fail")).toBeInTheDocument();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Queue the post-refresh PASSING fixture.
    fetchMock.mockResolvedValueOnce(makeFetchResponse(PASSING_FIXTURE));

    // Advance the (faked) interval 5 minutes — the setInterval callback
    // fires; the underlying fetch + setState use real microtasks, which
    // ``waitFor`` polls via real setTimeout.
    await act(async () => {
      vi.advanceTimersByTime(5 * 60 * 1000);
    });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(screen.getByTestId("drift-banner-pass")).toBeInTheDocument();
    });
  });
});
