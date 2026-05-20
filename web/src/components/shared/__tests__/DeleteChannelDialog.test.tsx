/**
 * DeleteChannelDialog tests.
 *
 * Guards: confirm button gating, syncing warning, chat vs file copy.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// ---------------------------------------------------------------------------
// Mocks — must be declared before the component import.
// ---------------------------------------------------------------------------

const mockRemove = vi.fn();
vi.mock("@/hooks/useDeleteChannel", () => ({
  useDeleteChannel: () => ({
    remove: mockRemove,
    loading: false,
    error: null,
  }),
}));

vi.mock("@/contexts/SyncStatusContext", () => ({
  useSyncStatus: () => ({
    syncingChannels: new Set<string>(),
    claim: vi.fn(),
    release: vi.fn(),
  }),
}));

// ToastViewport just needs to render without exploding.
vi.mock("@/components/settings/ToastViewport", () => ({
  ToastViewport: () => null,
}));

// useToast — provide a minimal stub so the dialog can call show().
vi.mock("@/hooks/useToast", () => ({
  useToast: () => ({
    toasts: [],
    show: vi.fn(),
    dismiss: vi.fn(),
  }),
}));

import { DeleteChannelDialog } from "../DeleteChannelDialog";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderDialog(props: Partial<React.ComponentProps<typeof DeleteChannelDialog>> = {}) {
  const defaults = {
    open: true,
    channelId: "ch-abc",
    channelName: "general",
    onClose: vi.fn(),
    onDeleted: vi.fn(),
  };
  return render(<DeleteChannelDialog {...defaults} {...props} />);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("DeleteChannelDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRemove.mockResolvedValue({ channel_id: "ch-abc", status: "completed" });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("does not render when open=false", () => {
    renderDialog({ open: false });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renders the dialog when open=true", () => {
    renderDialog();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(/Delete channel permanently/i)).toBeInTheDocument();
  });

  it("confirm button is disabled until name matches exactly", async () => {
    renderDialog({ channelName: "general" });
    const btn = screen.getByRole("button", { name: /Delete permanently/i });
    expect(btn).toBeDisabled();

    const input = screen.getByRole("textbox");
    await userEvent.type(input, "gene");
    expect(btn).toBeDisabled();

    await userEvent.clear(input);
    await userEvent.type(input, "general");
    expect(btn).not.toBeDisabled();
  });

  it("confirm button is disabled when input is only whitespace", async () => {
    renderDialog({ channelName: "general" });
    const btn = screen.getByRole("button", { name: /Delete permanently/i });
    const input = screen.getByRole("textbox");

    await userEvent.type(input, "   ");
    expect(btn).toBeDisabled();
  });

  it("shows syncing warning when isSyncing=true", () => {
    renderDialog({ isSyncing: true });
    expect(screen.getByTestId("syncing-warning")).toBeInTheDocument();
    expect(screen.getByText(/A sync is running/i)).toBeInTheDocument();
  });

  it("does NOT show syncing warning when isSyncing=false", () => {
    renderDialog({ isSyncing: false });
    expect(screen.queryByTestId("syncing-warning")).toBeNull();
  });

  it("shows chat copy by default (channelKind undefined)", () => {
    renderDialog({ channelName: "general", channelKind: undefined });
    expect(screen.getByText(/synced messages/i)).toBeInTheDocument();
    expect(screen.getByText(/wiki/i)).toBeInTheDocument();
  });

  it("shows file copy when channelKind='file'", () => {
    renderDialog({ channelName: "docs-import", channelKind: "file" });
    expect(screen.getByText(/imported documents/i)).toBeInTheDocument();
    // Uploaded files caveat
    expect(screen.getByText(/Uploaded files in storage may remain/i)).toBeInTheDocument();
  });

  it("shows chat copy when channelKind='chat'", () => {
    renderDialog({ channelName: "eng-chat", channelKind: "chat" });
    expect(screen.getByText(/synced messages/i)).toBeInTheDocument();
  });

  it("calls remove and onDeleted on successful submit", async () => {
    const onDeleted = vi.fn();
    renderDialog({ channelName: "general", onDeleted });

    const input = screen.getByRole("textbox");
    await userEvent.type(input, "general");

    const btn = screen.getByRole("button", { name: /Delete permanently/i });
    await userEvent.click(btn);

    await waitFor(() => {
      expect(mockRemove).toHaveBeenCalledWith("ch-abc", "general");
      expect(onDeleted).toHaveBeenCalledWith("ch-abc");
    });
  });

  it("calls onClose when Cancel is clicked", async () => {
    const onClose = vi.fn();
    renderDialog({ onClose });
    await userEvent.click(screen.getByRole("button", { name: /Cancel/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it("calls onClose when backdrop is clicked", async () => {
    const onClose = vi.fn();
    renderDialog({ onClose });
    // The backdrop is the outermost div[role=dialog]
    fireEvent.click(screen.getByRole("dialog"));
    expect(onClose).toHaveBeenCalled();
  });

  it("does not call onDeleted when remove throws", async () => {
    mockRemove.mockRejectedValue(new Error("network error"));
    const onDeleted = vi.fn();
    renderDialog({ channelName: "general", onDeleted });

    const input = screen.getByRole("textbox");
    await userEvent.type(input, "general");
    await userEvent.click(screen.getByRole("button", { name: /Delete permanently/i }));

    await waitFor(() => {
      expect(mockRemove).toHaveBeenCalled();
    });
    expect(onDeleted).not.toHaveBeenCalled();
  });
});
