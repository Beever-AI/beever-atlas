/**
 * RES-285 — SyncStatusContext behaviour tests.
 *
 * Specifically guards AC6: "Subscriber re-renders only when `isSyncRunning`
 * value changes." The whole point of splitting state into two primitive
 * `useState` cells (vs. one wrapped object) is to preserve React's
 * `Object.is` bail-out path. If a future refactor wraps these back into a
 * single object setter, this test will fail noisily.
 */

import { describe, it, expect } from "vitest";
import { act, render } from "@testing-library/react";
import { useRef } from "react";
import { SyncStatusProvider, useSyncStatus } from "../SyncStatusContext";

describe("SyncStatusContext (RES-285)", () => {
  it("default value is { isSyncRunning: false, channelId: null }", () => {
    let captured: { isSyncRunning: boolean; channelId: string | null } | null = null;
    function Probe() {
      const { isSyncRunning, channelId } = useSyncStatus();
      captured = { isSyncRunning, channelId };
      return null;
    }
    render(
      <SyncStatusProvider>
        <Probe />
      </SyncStatusProvider>,
    );
    expect(captured).toEqual({ isSyncRunning: false, channelId: null });
  });

  it("throws when used outside the provider", () => {
    function Probe() {
      useSyncStatus();
      return null;
    }
    // React 19 prints to console.error in addition to throwing. Capture
    // and assert only that we got the expected error type.
    const originalError = console.error;
    console.error = () => {};
    try {
      expect(() => render(<Probe />)).toThrow(/useSyncStatus must be used inside/);
    } finally {
      console.error = originalError;
    }
  });

  it("publishing an already-equal boolean does NOT re-render subscribers (AC6)", () => {
    let renderCount = 0;
    let setterRef: ((v: boolean) => void) | null = null;

    function Subscriber() {
      const { isSyncRunning, setIsSyncRunning } = useSyncStatus();
      const seen = useRef({ renders: 0 });
      seen.current.renders += 1;
      renderCount = seen.current.renders;
      setterRef = setIsSyncRunning;
      return <div data-testid="state">{String(isSyncRunning)}</div>;
    }

    render(
      <SyncStatusProvider>
        <Subscriber />
      </SyncStatusProvider>,
    );

    expect(renderCount).toBe(1);

    // Publish the same value (already false) — React's `Object.is` bail-out
    // must short-circuit this. If a refactor uses a single object setter,
    // this assertion will fail because `{...}` !== `{...}`.
    act(() => {
      setterRef!(false);
    });
    expect(renderCount).toBe(1);

    // Now publish a real change — exactly one re-render.
    act(() => {
      setterRef!(true);
    });
    expect(renderCount).toBe(2);

    // Same value again — still 2.
    act(() => {
      setterRef!(true);
    });
    expect(renderCount).toBe(2);
  });

  it("setIsSyncRunning and setChannelId are referentially stable across renders", () => {
    const seen = new Set<unknown>();
    let setterFromContext: ((v: boolean) => void) | null = null;

    function Probe() {
      const { setIsSyncRunning, setChannelId } = useSyncStatus();
      seen.add(setIsSyncRunning);
      seen.add(setChannelId);
      setterFromContext = setIsSyncRunning;
      return null;
    }

    const { rerender } = render(
      <SyncStatusProvider>
        <Probe />
      </SyncStatusProvider>,
    );

    // Force a re-render — setters must still be the same instances.
    rerender(
      <SyncStatusProvider>
        <Probe />
      </SyncStatusProvider>,
    );

    // Both setters captured across renders should de-dupe to exactly 2
    // entries (one isSync setter, one channelId setter).
    expect(seen.size).toBe(2);
    expect(typeof setterFromContext).toBe("function");
  });

  it("publishing channelId carries through to subscribers", () => {
    let captured: string | null = "<unset>";
    let setChannelIdRef: ((v: string | null) => void) | null = null;

    function Subscriber() {
      const { channelId, setChannelId } = useSyncStatus();
      captured = channelId;
      setChannelIdRef = setChannelId;
      return null;
    }

    render(
      <SyncStatusProvider>
        <Subscriber />
      </SyncStatusProvider>,
    );

    expect(captured).toBeNull();

    act(() => setChannelIdRef!("#marketing"));
    expect(captured).toBe("#marketing");

    act(() => setChannelIdRef!(null));
    expect(captured).toBeNull();
  });
});
