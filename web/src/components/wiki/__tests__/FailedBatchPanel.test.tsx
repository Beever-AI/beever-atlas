import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FailedBatchPanel } from "../FailedBatchPanel";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeFetchResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const ITEM_1 = {
  message_id: "msg-000001",
  next_attempt_at: new Date(Date.now() + 5 * 60 * 1000).toISOString(),
  attempt_count: 2,
  last_error: "Extraction LLM returned malformed JSON",
};

const ITEM_2 = {
  message_id: "msg-000002",
  next_attempt_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
  attempt_count: 5,
  last_error: "Timeout after 15000ms",
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("FailedBatchPanel", () => {
  it("renders rows from the extraction-failures endpoint", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse({ items: [ITEM_1, ITEM_2], next_cursor: null }),
    );

    render(<FailedBatchPanel channelId="ch-1" />);

    await waitFor(() => {
      expect(screen.getAllByTestId("failed-batch-row")).toHaveLength(2);
    });

    expect(screen.getByText("msg-000001")).toBeInTheDocument();
    expect(screen.getByText("msg-000002")).toBeInTheDocument();
  });

  it("renders message_id, attempt_count, and last_error for each row", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse({ items: [ITEM_1], next_cursor: null }),
    );

    render(<FailedBatchPanel channelId="ch-1" />);

    await waitFor(() => {
      expect(screen.getByTestId("failed-batch-row")).toBeInTheDocument();
    });

    expect(screen.getByText("msg-000001")).toBeInTheDocument();
    expect(screen.getByText(/2 attempt/i)).toBeInTheDocument();
    expect(screen.getByText(/malformed JSON/i)).toBeInTheDocument();
  });

  it("renders relative next_attempt_at time", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse({ items: [ITEM_1], next_cursor: null }),
    );

    render(<FailedBatchPanel channelId="ch-1" />);

    await waitFor(() => {
      expect(screen.getByText(/retry in \d+m/i)).toBeInTheDocument();
    });
  });

  it("shows Load more button when next_cursor is non-null", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse({ items: [ITEM_1], next_cursor: "cursor-abc" }),
    );

    render(<FailedBatchPanel channelId="ch-1" />);

    await waitFor(() => {
      expect(screen.getByTestId("load-more-button")).toBeInTheDocument();
    });
  });

  it("does not show Load more button when next_cursor is null", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse({ items: [ITEM_1], next_cursor: null }),
    );

    render(<FailedBatchPanel channelId="ch-1" />);

    await waitFor(() => {
      expect(screen.queryByTestId("load-more-button")).not.toBeInTheDocument();
    });
  });

  it("Load more advances cursor and appends rows", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock
      .mockResolvedValueOnce(
        makeFetchResponse({ items: [ITEM_1], next_cursor: "cursor-page2" }),
      )
      .mockResolvedValueOnce(
        makeFetchResponse({ items: [ITEM_2], next_cursor: null }),
      );

    render(<FailedBatchPanel channelId="ch-1" />);

    await waitFor(() => {
      expect(screen.getAllByTestId("failed-batch-row")).toHaveLength(1);
    });

    const user = userEvent.setup();
    await user.click(screen.getByTestId("load-more-button"));

    await waitFor(() => {
      expect(screen.getAllByTestId("failed-batch-row")).toHaveLength(2);
    });

    // Verify second fetch used the cursor
    const secondCall = fetchMock.mock.calls[1];
    const secondUrl = String(secondCall?.[0] ?? "");
    expect(secondUrl).toContain("cursor=cursor-page2");
  });

  it("shows empty state copy when zero rows returned", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse({ items: [], next_cursor: null }),
    );

    render(<FailedBatchPanel channelId="ch-1" />);

    await waitFor(() => {
      expect(screen.getByTestId("failed-batch-empty-state")).toBeInTheDocument();
    });

    expect(
      screen.getByText(/no failed extractions in the last 7 days/i),
    ).toBeInTheDocument();
  });

  it("calls onClose when close button is clicked", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      makeFetchResponse({ items: [], next_cursor: null }),
    );

    const onClose = vi.fn();
    render(<FailedBatchPanel channelId="ch-1" onClose={onClose} />);

    await waitFor(() => {
      expect(screen.getByTestId("failed-batch-empty-state")).toBeInTheDocument();
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /close panel/i }));

    expect(onClose).toHaveBeenCalledOnce();
  });
});
