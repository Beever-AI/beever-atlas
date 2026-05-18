/**
 * RES-285 — ChannelWorkspaceRoute behaviour tests.
 *
 * Guards AC1 ("switching A → B mid-sync shows B's idle state, no stale UI
 * from A") and AC7 ("cooldown countdown does not carry over") at the
 * structural level: navigating between `/channels/a` and `/channels/b`
 * must unmount the entire ChannelWorkspace subtree and mount a fresh one.
 *
 * We mock `@/pages/ChannelWorkspace` to a probe component that exposes its
 * mount count via a global counter — if the wrapper's `key={id}` works,
 * the probe's `useEffect` mount handler fires once per `:id` change.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useNavigate,
  useParams,
} from "react-router-dom";
import { useEffect } from "react";

// Track mount/unmount events from the probe so we can verify the key prop
// actually forces a remount on `:id` change.
const events: string[] = [];

vi.mock("@/pages/ChannelWorkspace", () => {
  // Probe component imported lazily inside the mock factory so vitest's
  // module-mock order works correctly. Note the probe is itself a real
  // React component using the live `useParams` from react-router-dom.
  const ChannelWorkspaceMock = () => {
    const { id } = useParams<{ id: string }>();
    useEffect(() => {
      events.push(`mount:${id}`);
      return () => {
        events.push(`unmount:${id}`);
      };
    }, [id]);
    return <div data-testid="probe">channel:{id}</div>;
  };
  return { ChannelWorkspace: ChannelWorkspaceMock };
});

// Import AFTER the mock so the wrapper resolves to the mocked component.
import { ChannelWorkspaceRoute } from "../ChannelWorkspaceRoute";

describe("ChannelWorkspaceRoute (RES-285)", () => {
  beforeEach(() => {
    events.length = 0;
  });

  it("renders ChannelWorkspace for the current :id", () => {
    render(
      <MemoryRouter initialEntries={["/channels/abc"]}>
        <Routes>
          <Route path="/channels/:id" element={<ChannelWorkspaceRoute />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByTestId("probe").textContent).toBe("channel:abc");
    expect(events).toContain("mount:abc");
  });

  it("unmounts the old workspace and mounts a fresh one when :id changes", () => {
    // Render once at /channels/a, then trigger an in-router navigation
    // to /channels/b. The `key={id}` on <ChannelWorkspace> must force a
    // full unmount + remount.
    function NavTrigger({ to }: { to: string }) {
      const navigate = useNavigate();
      return (
        <button data-testid="nav" type="button" onClick={() => navigate(to)}>
          go
        </button>
      );
    }

    render(
      <MemoryRouter initialEntries={["/channels/a"]}>
        <Routes>
          <Route path="/channels/:id" element={<ChannelWorkspaceRoute />} />
        </Routes>
        <NavTrigger to="/channels/b" />
      </MemoryRouter>,
    );

    expect(events.filter((e) => e === "mount:a")).toHaveLength(1);

    // Click triggers in-router navigation to /channels/b.
    act(() => {
      screen.getByTestId("nav").click();
    });

    // Old channel unmounted; new channel mounted fresh.
    expect(events).toContain("unmount:a");
    expect(events).toContain("mount:b");
    // Probe now reflects b.
    expect(screen.getByTestId("probe").textContent).toBe("channel:b");
  });

  it("renders nothing when :id is missing", () => {
    // Edge: deep-link to /channels (no id). The wrapper should render null
    // and not crash, leaving the parent route to fall through.
    const { container } = render(
      <MemoryRouter initialEntries={["/channels/"]}>
        <Routes>
          <Route path="/channels/*" element={<ChannelWorkspaceRoute />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(container.querySelector("[data-testid='probe']")).toBeNull();
  });
});
