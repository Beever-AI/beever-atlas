import { describe, it, expect, vi, beforeEach, type Mock } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { EditCredentialsDialog } from "../EditCredentialsDialog";
import type { PlatformConnection } from "@/lib/types";

// Mock useUpdateCredentials hook
vi.mock("@/hooks/useConnections", () => ({
  useUpdateCredentials: () => ({
    update: mockUpdate,
    loading: false,
    error: null,
  }),
}));

let mockUpdate: Mock<(id: string, credentials: Record<string, string>) => Promise<PlatformConnection>>;

function makeConnection(overrides: Partial<PlatformConnection> = {}): PlatformConnection {
  return {
    id: "conn-1",
    platform: "slack",
    display_name: "Engineering Workspace",
    status: "connected",
    error_message: null,
    selected_channels: ["C001", "C002"],
    source: "ui",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("EditCredentialsDialog", () => {
  let onClose: Mock<() => void>;
  let onSaved: Mock<() => void>;

  beforeEach(() => {
    onClose = vi.fn();
    onSaved = vi.fn();
    mockUpdate = vi.fn<(id: string, credentials: Record<string, string>) => Promise<PlatformConnection>>().mockResolvedValue(makeConnection());
  });

  it("renders the dialog with the platform name in the header", () => {
    render(
      <EditCredentialsDialog
        connection={makeConnection()}
        onClose={onClose}
        onSaved={onSaved}
      />,
    );
    expect(screen.getByText("Edit Slack credentials")).toBeTruthy();
  });

  it("Save is disabled when all fields are blank", () => {
    render(
      <EditCredentialsDialog
        connection={makeConnection()}
        onClose={onClose}
        onSaved={onSaved}
      />,
    );
    const saveBtn = screen.getByRole("button", { name: /save/i });
    expect((saveBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it("Save becomes enabled when at least one field is filled", () => {
    render(
      <EditCredentialsDialog
        connection={makeConnection()}
        onClose={onClose}
        onSaved={onSaved}
      />,
    );
    const passwordInputs = document.querySelectorAll('input[type="password"]');
    fireEvent.change(passwordInputs[0], { target: { value: "xoxb-test-token" } });
    const saveBtn = screen.getByRole("button", { name: /save/i });
    expect((saveBtn as HTMLButtonElement).disabled).toBe(false);
  });

  it("sends only non-empty fields and calls onSaved on success", async () => {
    render(
      <EditCredentialsDialog
        connection={makeConnection()}
        onClose={onClose}
        onSaved={onSaved}
      />,
    );
    const passwordInputs = document.querySelectorAll('input[type="password"]');
    // Fill only the first field (bot_token), leave others blank
    fireEvent.change(passwordInputs[0], { target: { value: "xoxb-new-token" } });

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
    const [id, credentials] = mockUpdate.mock.calls[0];
    expect(id).toBe("conn-1");
    // Only the filled field should be sent
    expect(credentials).toEqual({ bot_token: "xoxb-new-token" });
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
  });

  it("does not send blank fields in the credentials payload", async () => {
    render(
      <EditCredentialsDialog
        connection={makeConnection()}
        onClose={onClose}
        onSaved={onSaved}
      />,
    );
    const passwordInputs = document.querySelectorAll('input[type="password"]');
    // Fill bot_token and app_token, leave signing_secret blank
    fireEvent.change(passwordInputs[0], { target: { value: "xoxb-token" } });
    fireEvent.change(passwordInputs[1], { target: { value: "xapp-token" } });

    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
    const [, credentials] = mockUpdate.mock.calls[0];
    expect(credentials).toEqual({ bot_token: "xoxb-token", app_token: "xapp-token" });
    expect("signing_secret" in credentials).toBe(false);
  });

  it("calls onClose when Cancel is clicked", () => {
    render(
      <EditCredentialsDialog
        connection={makeConnection()}
        onClose={onClose}
        onSaved={onSaved}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("shows the app_token Socket Mode hint for Slack", () => {
    render(
      <EditCredentialsDialog
        connection={makeConnection({ platform: "slack" })}
        onClose={onClose}
        onSaved={onSaved}
      />,
    );
    expect(screen.getAllByText(/Socket Mode/).length).toBeGreaterThan(0);
  });
});
