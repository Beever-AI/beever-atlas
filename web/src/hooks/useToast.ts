import { useCallback, useRef, useState } from "react";

/** A single transient notification. */
export interface Toast {
  id: number;
  message: string;
  variant: "info" | "error";
}

export interface UseToastResult {
  toasts: Toast[];
  /** Push a toast. Info toasts auto-dismiss after ~2.5s; errors stay ~6s. */
  show: (message: string, variant?: Toast["variant"]) => void;
  /** Remove a toast by id (used by the close button on error toasts). */
  dismiss: (id: number) => void;
}

const INFO_TTL_MS = 2500;
const ERROR_TTL_MS = 6000;

/**
 * Tiny toast hook shared by the AI settings tabs. Mirrors the inline-toast
 * pattern that lived in ``AgentModelSettings`` (``fixed bottom-4 right-4``,
 * auto-dismiss), promoted to a hook so EndpointCard/AddEndpointPanel/AISetup
 * (and the future Embedding/Agent tabs) can all share it. Render the toasts
 * with ``<ToastViewport toasts={toasts} onDismiss={dismiss} />``.
 */
export function useToast(): UseToastResult {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(0);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const show = useCallback(
    (message: string, variant: Toast["variant"] = "info") => {
      const id = nextId.current++;
      setToasts((prev) => [...prev, { id, message, variant }]);
      const ttl = variant === "error" ? ERROR_TTL_MS : INFO_TTL_MS;
      window.setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, ttl);
    },
    [],
  );

  return { toasts, show, dismiss };
}
