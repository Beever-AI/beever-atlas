/**
 * RES-285 — SyncStatusContext behaviour tests.
 *
 * Guards the multi-channel `Set<string>` contract: many channels can
 * sync concurrently; the publisher protocol is claim/release; the
 * Provider's background poller eventually releases stale ids when
 * their syncs complete.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render } from "@testing-library/react";
import { useRef } from "react";
import { SyncStatusProvider, useSyncStatus } from "../SyncStatusContext";

// Mock the api helper so the Provider's background poller doesn't hit
// a real backend during tests.
vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn().mockResolvedValue({ state: "idle", phases: [] }),
  },
}));

describe("SyncStatusContext (RES-285)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("default state has an empty syncingChannels set", () => {
    let captured: ReadonlySet<string> | null = null;
    function Probe() {
      captured = useSyncStatus().syncingChannels;
      return null;
    }
    render(
      <SyncStatusProvider>
        <Probe />
      </SyncStatusProvider>,
    );
    expect(captured).not.toBeNull();
    expect(captured!.size).toBe(0);
  });

  it("throws when used outside the provider", () => {
    function Probe() {
      useSyncStatus();
      return null;
    }
    const originalError = console.error;
    console.error = () => {};
    try {
      expect(() => render(<Probe />)).toThrow(/useSyncStatus must be used inside/);
    } finally {
      console.error = originalError;
    }
  });

  it("claim() adds a channel id; release() removes it", () => {
    let claimRef: ((id: string) => void) | null = null;
    let releaseRef: ((id: string) => void) | null = null;
    let captured: ReadonlySet<string> = new Set();

    function Probe() {
      const { syncingChannels, claim, release } = useSyncStatus();
      captured = syncingChannels;
      claimRef = claim;
      releaseRef = release;
      return null;
    }

    render(
      <SyncStatusProvider>
        <Probe />
      </SyncStatusProvider>,
    );

    expect(captured.has("ch-a")).toBe(false);

    act(() => claimRef!("ch-a"));
    expect(captured.has("ch-a")).toBe(true);
    expect(captured.size).toBe(1);

    act(() => releaseRef!("ch-a"));
    expect(captured.has("ch-a")).toBe(false);
    expect(captured.size).toBe(0);
  });

  it("supports multiple concurrent syncs", () => {
    let claimRef: ((id: string) => void) | null = null;
    let releaseRef: ((id: string) => void) | null = null;
    let captured: ReadonlySet<string> = new Set();

    function Probe() {
      const { syncingChannels, claim, release } = useSyncStatus();
      captured = syncingChannels;
      claimRef = claim;
      releaseRef = release;
      return null;
    }

    render(
      <SyncStatusProvider>
        <Probe />
      </SyncStatusProvider>,
    );

    act(() => {
      claimRef!("ch-a");
      claimRef!("ch-b");
      claimRef!("ch-c");
    });
    expect(captured.size).toBe(3);
    expect(captured.has("ch-a")).toBe(true);
    expect(captured.has("ch-b")).toBe(true);
    expect(captured.has("ch-c")).toBe(true);

    act(() => releaseRef!("ch-b"));
    expect(captured.size).toBe(2);
    expect(captured.has("ch-a")).toBe(true);
    expect(captured.has("ch-b")).toBe(false);
    expect(captured.has("ch-c")).toBe(true);
  });

  it("claim() is idempotent — duplicate claim does NOT re-render (Set identity stable)", () => {
    let renderCount = 0;
    let claimRef: ((id: string) => void) | null = null;

    function Subscriber() {
      const { syncingChannels, claim } = useSyncStatus();
      const seen = useRef({ renders: 0 });
      seen.current.renders += 1;
      renderCount = seen.current.renders;
      claimRef = claim;
      return <div data-testid="size">{syncingChannels.size}</div>;
    }

    render(
      <SyncStatusProvider>
        <Subscriber />
      </SyncStatusProvider>,
    );
    expect(renderCount).toBe(1);

    act(() => claimRef!("ch-a"));
    expect(renderCount).toBe(2); // membership changed: one re-render

    // Idempotent — claim() again with the same id must NOT re-render
    // (Set is returned unchanged via the `return prev` no-op branch).
    act(() => claimRef!("ch-a"));
    expect(renderCount).toBe(2);
  });

  it("release() is idempotent — releasing an id we never claimed is a no-op", () => {
    let renderCount = 0;
    let releaseRef: ((id: string) => void) | null = null;

    function Subscriber() {
      const { release } = useSyncStatus();
      const seen = useRef({ renders: 0 });
      seen.current.renders += 1;
      renderCount = seen.current.renders;
      releaseRef = release;
      return null;
    }

    render(
      <SyncStatusProvider>
        <Subscriber />
      </SyncStatusProvider>,
    );
    expect(renderCount).toBe(1);

    act(() => releaseRef!("ch-never-claimed"));
    expect(renderCount).toBe(1); // no membership change → no re-render
  });

  it("claim() and release() are referentially stable across renders", () => {
    const seen = new Set<unknown>();

    function Probe() {
      const { claim, release } = useSyncStatus();
      seen.add(claim);
      seen.add(release);
      return null;
    }

    const { rerender } = render(
      <SyncStatusProvider>
        <Probe />
      </SyncStatusProvider>,
    );

    rerender(
      <SyncStatusProvider>
        <Probe />
      </SyncStatusProvider>,
    );

    // Both setters captured across two renders → exactly 2 distinct
    // function references.
    expect(seen.size).toBe(2);
  });
});
