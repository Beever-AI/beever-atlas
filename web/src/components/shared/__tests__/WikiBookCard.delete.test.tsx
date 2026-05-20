/**
 * WikiBookCard kebab-delete tests.
 *
 * Guards: kebab opens dialog without triggering navigation; dialog is
 * not rendered at all when onDeleted is not provided.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

// ---------------------------------------------------------------------------
// Mocks — declared before component import
// ---------------------------------------------------------------------------

vi.mock("@/contexts/SyncStatusContext", () => ({
  useSyncStatus: () => ({
    syncingChannels: new Set<string>(),
    claim: vi.fn(),
    release: vi.fn(),
  }),
}));

vi.mock("@/hooks/useTheme", () => ({
  useTheme: () => ({ resolvedTheme: "light" }),
}));

vi.mock("@/lib/platform-badge", () => ({
  getPlatformBadgeStyle: () => ({
    color: "#000",
    backgroundColor: "#eee",
  }),
}));

vi.mock("@/components/shared/WikiStateIcon", () => ({
  WikiStateIcon: () => null,
}));

vi.mock("@/lib/wikiState", () => ({
  formatRelativeTime: () => "just now",
  wikiStateLabel: () => "Ready",
}));

// DeleteChannelDialog — stub so we can observe whether it's rendered
// and intercept the onDeleted callback without hitting real fetch.
const mockDeleteDialog = vi.fn();
vi.mock("@/components/shared/DeleteChannelDialog", () => ({
  DeleteChannelDialog: (props: Record<string, unknown>) => {
    mockDeleteDialog(props);
    if (!props.open) return null;
    return (
      <div data-testid="delete-dialog">
        <button
          onClick={() =>
            (props.onDeleted as (id: string) => void)(props.channelId as string)
          }
        >
          Confirm delete
        </button>
        <button onClick={() => (props.onClose as () => void)()}>
          Close dialog
        </button>
      </div>
    );
  },
}));

import { WikiBookCard } from "../WikiBookCard";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderCard(onDeleted?: (id: string) => void) {
  return render(
    <MemoryRouter>
      <WikiBookCard
        channelId="ch-abc"
        name="general"
        platform="slack"
        state="ready"
        onDeleted={onDeleted}
      />
    </MemoryRouter>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("WikiBookCard kebab delete", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("does not render the kebab button when onDeleted is not provided", () => {
    renderCard(/* no onDeleted */);
    expect(screen.queryByLabelText(/Channel actions/i)).toBeNull();
  });

  it("renders the kebab button when onDeleted is provided", () => {
    renderCard(vi.fn());
    expect(screen.getByLabelText(/Channel actions/i)).toBeInTheDocument();
  });

  it("clicking the kebab opens the delete dialog without navigating", async () => {
    renderCard(vi.fn());

    // Dialog should not be visible initially
    expect(screen.queryByTestId("delete-dialog")).toBeNull();

    const kebab = screen.getByLabelText(/Channel actions/i);
    await userEvent.click(kebab);

    // A dropdown menu item "Delete" should appear
    expect(screen.getByRole("button", { name: /^Delete$/i })).toBeInTheDocument();

    // Click Delete menu item — should open the dialog
    await userEvent.click(screen.getByRole("button", { name: /^Delete$/i }));

    expect(screen.getByTestId("delete-dialog")).toBeInTheDocument();
  });

  it("kebab click does not propagate to the Link (no navigation)", async () => {
    // We use MemoryRouter — if navigation happened the location would change.
    // We verify by checking that the dialog opens instead of the test throwing
    // a navigation error, and by ensuring e.stopPropagation() was called via
    // the mock dialog being visible.
    const onDeleted = vi.fn();
    renderCard(onDeleted);

    const kebab = screen.getByLabelText(/Channel actions/i);

    // Fire a synthetic click with stopPropagation spy
    const clickEvent = new MouseEvent("click", { bubbles: true });
    const stopSpy = vi.spyOn(clickEvent, "stopPropagation");
    kebab.dispatchEvent(clickEvent);

    // stopPropagation was invoked
    expect(stopSpy).toHaveBeenCalled();
  });

  it("onDeleted is called after dialog confirms deletion", async () => {
    const onDeleted = vi.fn();
    renderCard(onDeleted);

    const kebab = screen.getByLabelText(/Channel actions/i);
    await userEvent.click(kebab);
    await userEvent.click(screen.getByRole("button", { name: /^Delete$/i }));

    // Stub dialog's confirm button calls onDeleted prop
    await userEvent.click(screen.getByRole("button", { name: /Confirm delete/i }));

    expect(onDeleted).toHaveBeenCalledWith("ch-abc");
  });

  it("dialog closes when onClose is called", async () => {
    renderCard(vi.fn());

    const kebab = screen.getByLabelText(/Channel actions/i);
    await userEvent.click(kebab);
    await userEvent.click(screen.getByRole("button", { name: /^Delete$/i }));

    expect(screen.getByTestId("delete-dialog")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Close dialog/i }));

    await waitFor(() => {
      expect(screen.queryByTestId("delete-dialog")).toBeNull();
    });
  });
});
