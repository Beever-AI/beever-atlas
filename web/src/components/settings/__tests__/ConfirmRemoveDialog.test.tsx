import { describe, it, expect, vi, beforeEach, type Mock } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ConfirmRemoveDialog } from "../ConfirmRemoveDialog";
import type { PlatformConnection } from "@/lib/types";

function makeConnection(overrides: Partial<PlatformConnection> = {}): PlatformConnection {
  return {
    id: "conn-1",
    platform: "slack",
    display_name: "Engineering Workspace",
    status: "connected",
    error_message: null,
    selected_channels: ["C001"],
    source: "ui",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("ConfirmRemoveDialog", () => {
  let onCancel: Mock<() => void>;
  let onConfirm: Mock<(cascade: boolean) => void>;

  beforeEach(() => {
    onCancel = vi.fn();
    onConfirm = vi.fn();
  });

  it("renders the connection display_name in the title", () => {
    render(
      <ConfirmRemoveDialog
        connection={makeConnection()}
        onCancel={onCancel}
        onConfirm={onConfirm}
      />,
    );
    expect(screen.getByText("Remove Engineering Workspace?")).toBeTruthy();
  });

  it("falls back to platform name when display_name is empty", () => {
    render(
      <ConfirmRemoveDialog
        connection={makeConnection({ display_name: "" })}
        onCancel={onCancel}
        onConfirm={onConfirm}
      />,
    );
    expect(screen.getByText("Remove slack?")).toBeTruthy();
  });

  it("calls onConfirm(true) when 'Remove & delete data' is clicked", () => {
    render(
      <ConfirmRemoveDialog
        connection={makeConnection()}
        onCancel={onCancel}
        onConfirm={onConfirm}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /remove.*delete data/i }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith(true);
  });

  it("calls onConfirm(false) when 'Remove, keep data' is clicked", () => {
    render(
      <ConfirmRemoveDialog
        connection={makeConnection()}
        onCancel={onCancel}
        onConfirm={onConfirm}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /remove, keep data/i }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith(false);
  });

  it("calls onCancel when Cancel button is clicked", () => {
    render(
      <ConfirmRemoveDialog
        connection={makeConnection()}
        onCancel={onCancel}
        onConfirm={onConfirm}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("shows the data loss warning text", () => {
    render(
      <ConfirmRemoveDialog
        connection={makeConnection()}
        onCancel={onCancel}
        onConfirm={onConfirm}
      />,
    );
    expect(screen.getByText(/permanently delete/i)).toBeTruthy();
  });
});
