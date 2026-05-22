/**
 * WorkspaceGroup — progressive-disclosure ("N more without a wiki") tests.
 *
 * Guards:
 *  - channels WITH a wiki (ready/building) show by default; no-wiki ones are
 *    hidden behind the expander.
 *  - the expander label reports the correct hidden count and toggles the rows.
 *  - a currently-syncing no-wiki channel stays pinned above the fold.
 *  - a 0-wiki workspace shows "N channels · no wiki yet" (no bare header).
 *  - the expander exposes aria-expanded for a11y.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { WikiState } from "@/hooks/useWikiStates";

// Configurable syncing set, hoisted so the vi.mock factory can read it.
const h = vi.hoisted(() => ({ syncing: new Set<string>() }));

vi.mock("@/contexts/SyncStatusContext", () => ({
  useSyncStatus: () => ({ syncingChannels: h.syncing, claim: vi.fn(), release: vi.fn() }),
}));
vi.mock("@/components/shared/WikiStateIcon", () => ({ WikiStateIcon: () => null }));
vi.mock("@/components/shared/PlatformIcon", () => ({ PlatformIcon: () => null }));
vi.mock("@/components/channel/FavoriteButton", () => ({ FavoriteButton: () => null }));
vi.mock("@/components/ui/tooltip", () => ({
  Tooltip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipTrigger: ({ render }: { render: React.ReactElement }) => render,
  // Render nothing for tooltip content — otherwise each channel name appears
  // twice (row + tooltip) and getByText finds duplicates.
  TooltipContent: () => null,
}));

import { WorkspaceGroup } from "../WorkspaceGroup";

type Ch = {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  connection_id: string | null;
};

function ch(id: string, name: string): Ch {
  return { channel_id: id, name, platform: "discord", is_member: true, member_count: 1, connection_id: "conn-1" };
}

function renderGroup(
  channels: Ch[],
  states: Record<string, WikiState>,
  opts?: { disableFold?: boolean },
) {
  return render(
    <MemoryRouter>
      <WorkspaceGroup
        label="DC"
        platform="discord"
        channels={channels}
        defaultCollapsed={false}
        onToggleCollapse={vi.fn()}
        isFavorite={() => false}
        onToggleFavorite={vi.fn()}
        getWikiState={(id) => states[id] ?? "empty"}
        disableFold={opts?.disableFold}
      />
    </MemoryRouter>,
  );
}

describe("WorkspaceGroup progressive disclosure", () => {
  it("shows wiki channels and hides no-wiki ones behind the expander", async () => {
    h.syncing = new Set();
    renderGroup(
      [ch("a", "noise"), ch("b", "wanted-backups"), ch("c", "bot-commands"), ch("d", "supplies")],
      { a: "ready", b: "ready", c: "empty", d: "empty" },
    );

    expect(screen.getByText("noise")).toBeInTheDocument();
    expect(screen.getByText("wanted-backups")).toBeInTheDocument();
    expect(screen.queryByText("bot-commands")).not.toBeInTheDocument();
    expect(screen.queryByText("supplies")).not.toBeInTheDocument();

    const expander = screen.getByRole("button", { name: /2 more without a wiki/i });
    expect(expander).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(expander);

    expect(screen.getByText("bot-commands")).toBeInTheDocument();
    expect(screen.getByText("supplies")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /hide 2 without a wiki/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("keeps a currently-syncing no-wiki channel pinned above the fold", () => {
    h.syncing = new Set(["c"]); // 'bot-commands' is syncing despite no wiki
    renderGroup(
      [ch("a", "noise"), ch("c", "bot-commands"), ch("d", "supplies")],
      { a: "ready", c: "empty", d: "empty" },
    );

    // syncing channel visible without expanding
    expect(screen.getByText("bot-commands")).toBeInTheDocument();
    // the other no-wiki channel is still hidden, and the count excludes the syncing one
    expect(screen.queryByText("supplies")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /1 more without a wiki/i })).toBeInTheDocument();
  });

  it("labels a 0-wiki workspace as 'N channels · no wiki yet'", () => {
    h.syncing = new Set();
    renderGroup([ch("c", "bot-commands"), ch("d", "supplies"), ch("e", "events")], {
      c: "empty",
      d: "empty",
      e: "empty",
    });

    expect(screen.getByRole("button", { name: /3 channels · no wiki yet/i })).toBeInTheDocument();
    // nothing above the fold until expanded
    expect(screen.queryByText("bot-commands")).not.toBeInTheDocument();
  });

  it("does not render an expander when every channel has a wiki", () => {
    h.syncing = new Set();
    renderGroup([ch("a", "noise"), ch("b", "wanted-backups")], { a: "ready", b: "building" });

    expect(screen.getByText("noise")).toBeInTheDocument();
    expect(screen.getByText("wanted-backups")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /without a wiki/i })).not.toBeInTheDocument();
  });

  it("disableFold (search path) shows every channel and no expander", () => {
    h.syncing = new Set();
    renderGroup(
      [ch("a", "noise"), ch("c", "bot-commands"), ch("d", "supplies")],
      { a: "ready", c: "empty", d: "empty" },
      { disableFold: true },
    );

    // all matches visible without expanding — a search must never hide a hit
    expect(screen.getByText("noise")).toBeInTheDocument();
    expect(screen.getByText("bot-commands")).toBeInTheDocument();
    expect(screen.getByText("supplies")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /without a wiki/i })).not.toBeInTheDocument();
  });

  it("uses the singular '1 channel · no wiki yet' label", () => {
    h.syncing = new Set();
    renderGroup([ch("c", "bot-commands")], { c: "empty" });

    expect(screen.getByRole("button", { name: /1 channel · no wiki yet/i })).toBeInTheDocument();
  });

  it("puts errored-wiki channels below the fold", () => {
    h.syncing = new Set();
    renderGroup([ch("a", "noise"), ch("e", "broken")], { a: "ready", e: "errored" });

    expect(screen.queryByText("broken")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /1 more without a wiki/i })).toBeInTheDocument();
  });
});
