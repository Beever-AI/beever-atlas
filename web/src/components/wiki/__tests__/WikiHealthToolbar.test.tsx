import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { WikiHealthToolbar } from "../WikiHealthToolbar";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockRunLint = vi.fn();
const mockLintClear = vi.fn();
const mockLintState = {
  report: null as null | { findings: { severity: string; page_id: string; section_id?: string; message: string; suggested_action?: string; category: string }[]; pages_scanned: number; channel_id: string; target_lang: string; generated_at: string },
  loading: false,
  error: null as string | null,
  runLint: mockRunLint,
  clear: mockLintClear,
};

vi.mock("@/hooks/useWikiLint", () => ({
  useWikiLint: () => mockLintState,
}));

vi.mock("../FailedBatchPanel", () => ({
  FailedBatchPanel: ({ onClose }: { channelId: string; onClose?: () => void }) => (
    <div data-testid="failed-batch-panel">
      <button onClick={onClose}>Close</button>
    </div>
  ),
}));

vi.mock("@/components/ui/tooltip", () => ({
  Tooltip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipTrigger: ({
    children,
    className,
    onClick,
    disabled,
    "aria-label": ariaLabel,
    ...rest
  }: React.ButtonHTMLAttributes<HTMLButtonElement> & { children?: React.ReactNode }) => (
    <button
      type="button"
      aria-label={ariaLabel}
      className={className}
      onClick={onClick}
      disabled={disabled}
      {...rest}
    >
      {children}
    </button>
  ),
  TooltipContent: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="tooltip-content" hidden>{children}</div>
  ),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderToolbar(
  props: Partial<{
    versionCount: number;
    failureCount: number;
    onDownload: () => void;
    onHistoryToggle: () => void;
    onReorganize: () => void;
    onRebuild: () => void;
    isRegenerating: boolean;
    activeSlug: string;
    activePagePinned: boolean;
    activePageHidden: boolean;
    onPinToggle: (pinned: boolean) => void;
    onHideToggle: (hidden: boolean) => void;
    onSplit: (title: string) => void;
    onMerge: (slug: string) => void;
  }> = {},
) {
  return render(
    <MemoryRouter initialEntries={["/channels/ch-1/wiki"]}>
      <WikiHealthToolbar
        channelId="ch-1"
        versionCount={props.versionCount ?? 0}
        failureCount={props.failureCount}
        onDownload={props.onDownload}
        onHistoryToggle={props.onHistoryToggle}
        onReorganize={props.onReorganize}
        onRebuild={props.onRebuild}
        isRegenerating={props.isRegenerating ?? false}
        activeSlug={props.activeSlug}
        activePagePinned={props.activePagePinned}
        activePageHidden={props.activePageHidden}
        onPinToggle={props.onPinToggle}
        onHideToggle={props.onHideToggle}
        onSplit={props.onSplit}
        onMerge={props.onMerge}
      />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockLintState.report = null;
  mockLintState.loading = false;
  mockLintState.error = null;
  mockRunLint.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Action redesign — Maintain folded into the footer "Update wiki" button
// (these regression guards verify the button is gone from this toolbar).
// ---------------------------------------------------------------------------

describe("WikiHealthToolbar — Maintain removal", () => {
  it("does NOT render a Maintain Wiki menu item (folded into Update wiki)", async () => {
    renderToolbar();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(
      screen.queryByRole("menuitem", { name: /maintain wiki/i }),
    ).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tools menu
// ---------------------------------------------------------------------------

describe("WikiHealthToolbar — Tools menu", () => {
  it("renders Tools button always", () => {
    renderToolbar();
    expect(screen.getByRole("button", { name: /wiki tools menu/i })).toBeInTheDocument();
  });

  it("Tools menu is closed by default", () => {
    renderToolbar();
    expect(screen.queryByRole("menu", { name: /wiki tools/i })).not.toBeInTheDocument();
  });

  it("opens menu when Tools button is clicked", async () => {
    renderToolbar();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(screen.getByRole("menu", { name: /wiki tools/i })).toBeInTheDocument();
  });

  it("menu contains Health check, History, Download items", async () => {
    renderToolbar({ versionCount: 3 });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(screen.getByRole("menuitem", { name: /health check/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /version history/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /download wiki/i })).toBeInTheDocument();
  });

  it("Failures item is visible when failureCount > 0", async () => {
    renderToolbar({ failureCount: 3 });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(screen.getByRole("menuitem", { name: /view failed extractions/i })).toBeInTheDocument();
  });

  it("Failures item is hidden when failureCount is 0", async () => {
    renderToolbar({ failureCount: 0 });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(screen.queryByRole("menuitem", { name: /view failed extractions/i })).not.toBeInTheDocument();
  });

  it("Failures item is visible when failureCount is undefined (unknown)", async () => {
    renderToolbar({ failureCount: undefined });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(screen.getByRole("menuitem", { name: /view failed extractions/i })).toBeInTheDocument();
  });

  it("Reorganize folders item is shown when onReorganize is provided", async () => {
    renderToolbar({ onReorganize: vi.fn() });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(
      screen.getByRole("menuitem", { name: /reorganize folders/i }),
    ).toBeInTheDocument();
  });

  it("Reorganize click fires onReorganize directly (no confirm)", async () => {
    const onReorganize = vi.fn();
    renderToolbar({ onReorganize });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /reorganize folders/i }));
    expect(onReorganize).toHaveBeenCalledTimes(1);
  });

  it("Rebuild from scratch item is shown when onRebuild is provided", async () => {
    renderToolbar({ onRebuild: vi.fn() });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(
      screen.getByRole("menuitem", { name: /rebuild wiki from scratch/i }),
    ).toBeInTheDocument();
  });

  it("Rebuild shows confirm dialog before firing", async () => {
    const onRebuild = vi.fn();
    renderToolbar({ onRebuild });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /rebuild wiki from scratch/i }));
    expect(screen.getByRole("button", { name: /confirm rebuild/i })).toBeInTheDocument();
    expect(onRebuild).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /confirm rebuild/i }));
    expect(onRebuild).toHaveBeenCalledTimes(1);
  });

  it("Cancel on rebuild confirm hides the confirm prompt", async () => {
    renderToolbar({ onRebuild: vi.fn() });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /rebuild wiki from scratch/i }));
    await user.click(screen.getByRole("button", { name: /cancel rebuild/i }));
    expect(screen.queryByRole("button", { name: /confirm rebuild/i })).not.toBeInTheDocument();
  });

  it("Health check click runs lint and opens findings panel", async () => {
    mockRunLint.mockImplementation(() => {
      mockLintState.report = {
        findings: [],
        pages_scanned: 3,
        channel_id: "ch-1",
        target_lang: "en",
        generated_at: new Date().toISOString(),
      };
      return Promise.resolve();
    });

    renderToolbar();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /health check/i }));

    await waitFor(() => {
      expect(screen.getByRole("dialog", { name: /lint findings/i })).toBeInTheDocument();
    });
  });

  it("Download calls onDownload", async () => {
    const onDownload = vi.fn();
    renderToolbar({ onDownload });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /download wiki/i }));
    expect(onDownload).toHaveBeenCalledTimes(1);
  });

  it("History calls onHistoryToggle", async () => {
    const onHistoryToggle = vi.fn();
    renderToolbar({ onHistoryToggle, versionCount: 2 });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /version history/i }));
    expect(onHistoryToggle).toHaveBeenCalledTimes(1);
  });

  it("Failures click opens FailedBatchPanel", async () => {
    renderToolbar({ failureCount: 5 });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /view failed extractions/i }));
    await waitFor(() => {
      expect(screen.getByTestId("failed-batch-panel")).toBeInTheDocument();
    });
  });

  it("menu closes on Escape", async () => {
    renderToolbar();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Lint findings panel
// ---------------------------------------------------------------------------

describe("WikiHealthToolbar — Lint findings panel", () => {
  it("shows loading skeleton during lint scan with role=status and aria-live", async () => {
    mockLintState.loading = false;
    mockLintState.report = {
      findings: [],
      pages_scanned: 4,
      channel_id: "ch-1",
      target_lang: "en",
      generated_at: new Date().toISOString(),
    };

    const { rerender } = renderToolbar();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /health check/i }));

    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });

    mockLintState.loading = true;
    rerender(
      <MemoryRouter initialEntries={["/channels/ch-1/wiki"]}>
        <WikiHealthToolbar channelId="ch-1" />
      </MemoryRouter>,
    );

    const status = screen.getByRole("status");
    expect(status).toBeInTheDocument();
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("shows retry button on lint error", async () => {
    mockLintState.error = "Network error";
    mockLintState.loading = false;

    renderToolbar();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /health check/i }));

    expect(screen.getByRole("button", { name: /retry lint/i })).toBeInTheDocument();
  });

  it("retry button calls runLint again", async () => {
    mockLintState.error = "Network error";
    mockLintState.loading = false;

    renderToolbar();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /health check/i }));
    await user.click(screen.getByRole("button", { name: /retry lint/i }));

    expect(mockRunLint).toHaveBeenCalledTimes(2);
  });

  it("opens findings panel with role=dialog after lint completes", async () => {
    mockRunLint.mockImplementation(() => {
      mockLintState.report = {
        findings: [],
        pages_scanned: 3,
        channel_id: "ch-1",
        target_lang: "en",
        generated_at: new Date().toISOString(),
      };
      return Promise.resolve();
    });

    renderToolbar();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /health check/i }));

    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-label", "Lint findings");
  });

  it("shows no-issues message when findings is empty", async () => {
    mockLintState.report = {
      findings: [],
      pages_scanned: 2,
      channel_id: "ch-1",
      target_lang: "en",
      generated_at: new Date().toISOString(),
    };

    renderToolbar();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /health check/i }));

    await waitFor(() => {
      expect(screen.getByText(/no issues/i)).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// §5.15 / §5.16 — curation menu items
// ---------------------------------------------------------------------------

describe("WikiHealthToolbar — curation items (§5.15 / §5.16)", () => {
  it("does NOT render Pin/Hide/Split/Merge when no activeSlug", async () => {
    renderToolbar({});
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(screen.queryByRole("menuitem", { name: /^Pin/i })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /^Hide/i })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /^Split/i })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /^Merge/i })).toBeNull();
  });

  it("renders all four curation items when activeSlug is set", async () => {
    renderToolbar({
      activeSlug: "topic-auth",
      onPinToggle: vi.fn(),
      onHideToggle: vi.fn(),
      onSplit: vi.fn(),
      onMerge: vi.fn(),
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(screen.getByRole("menuitem", { name: /Pin this page/i })).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: /Hide this page/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Split this page/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Merge another page/i })).toBeInTheDocument();
  });

  it("Pin click fires onPinToggle(true) and item label flips when pinned=true", async () => {
    const onPinToggle = vi.fn();
    const { rerender } = renderToolbar({
      activeSlug: "topic-auth",
      activePagePinned: false,
      onPinToggle,
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /Pin this page/i }));
    expect(onPinToggle).toHaveBeenCalledTimes(1);
    expect(onPinToggle).toHaveBeenCalledWith(true);

    rerender(
      <MemoryRouter initialEntries={["/channels/ch-1/wiki"]}>
        <WikiHealthToolbar
          channelId="ch-1"
          activeSlug="topic-auth"
          activePagePinned={true}
          onPinToggle={onPinToggle}
        />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(screen.getByRole("menuitem", { name: /Unpin this page/i })).toBeInTheDocument();
  });

  it("Hide click fires onHideToggle(true) and label flips when hidden=true", async () => {
    const onHideToggle = vi.fn();
    const { rerender } = renderToolbar({
      activeSlug: "topic-auth",
      activePageHidden: false,
      onHideToggle,
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /Hide this page/i }));
    expect(onHideToggle).toHaveBeenCalledWith(true);

    rerender(
      <MemoryRouter initialEntries={["/channels/ch-1/wiki"]}>
        <WikiHealthToolbar
          channelId="ch-1"
          activeSlug="topic-auth"
          activePageHidden={true}
          onHideToggle={onHideToggle}
        />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    expect(screen.getByRole("menuitem", { name: /Show this page/i })).toBeInTheDocument();
  });

  it("Split click opens the split modal; Confirm fires onSplit", async () => {
    const onSplit = vi.fn();
    renderToolbar({ activeSlug: "topic-auth", onSplit });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /Split this page/i }));
    const dialog = screen.getByRole("dialog", { name: /split wiki page/i });
    expect(dialog).toBeInTheDocument();
    const input = screen.getByLabelText(/new page title/i);
    await user.type(input, "Auth — Session Policy");
    await user.click(screen.getByRole("button", { name: /confirm split/i }));
    expect(onSplit).toHaveBeenCalledWith("Auth — Session Policy");
  });

  it("Merge click opens the merge modal; Confirm fires onMerge", async () => {
    const onMerge = vi.fn();
    renderToolbar({ activeSlug: "topic-auth", onMerge });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /Merge another page/i }));
    const dialog = screen.getByRole("dialog", { name: /merge wiki page/i });
    expect(dialog).toBeInTheDocument();
    const input = screen.getByLabelText(/source slug/i);
    await user.type(input, "topic-auth-old");
    await user.click(screen.getByRole("button", { name: /confirm merge/i }));
    expect(onMerge).toHaveBeenCalledWith("topic-auth-old");
  });

  it("Confirm Split is disabled when title is empty (regression guard)", async () => {
    const onSplit = vi.fn();
    renderToolbar({ activeSlug: "topic-auth", onSplit });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /wiki tools menu/i }));
    await user.click(screen.getByRole("menuitem", { name: /Split this page/i }));
    const confirmBtn = screen.getByRole("button", { name: /confirm split/i });
    expect(confirmBtn).toBeDisabled();
    await user.click(confirmBtn);
    expect(onSplit).not.toHaveBeenCalled();
  });
});
