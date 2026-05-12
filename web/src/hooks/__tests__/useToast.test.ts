import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useToast } from "../useToast";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useToast", () => {
  it("shows an info toast and auto-dismisses it after ~2.5s", () => {
    const { result } = renderHook(() => useToast());
    act(() => result.current.show("saved"));
    expect(result.current.toasts).toHaveLength(1);
    expect(result.current.toasts[0].variant).toBe("info");
    expect(result.current.toasts[0].message).toBe("saved");

    act(() => vi.advanceTimersByTime(2600));
    expect(result.current.toasts).toHaveLength(0);
  });

  it("error toasts linger longer than info toasts", () => {
    const { result } = renderHook(() => useToast());
    act(() => result.current.show("boom", "error"));
    expect(result.current.toasts[0].variant).toBe("error");

    act(() => vi.advanceTimersByTime(3000));
    expect(result.current.toasts).toHaveLength(1); // still there past info TTL

    act(() => vi.advanceTimersByTime(4000));
    expect(result.current.toasts).toHaveLength(0);
  });

  it("stacks multiple toasts and supports manual dismiss", () => {
    const { result } = renderHook(() => useToast());
    act(() => {
      result.current.show("one");
      result.current.show("two", "error");
    });
    expect(result.current.toasts).toHaveLength(2);

    const errorId = result.current.toasts[1].id;
    act(() => result.current.dismiss(errorId));
    expect(result.current.toasts).toHaveLength(1);
    expect(result.current.toasts[0].message).toBe("one");
  });
});
