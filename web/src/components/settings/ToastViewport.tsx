import { AlertTriangle, CheckCircle2, X } from "lucide-react";
import type { Toast } from "@/hooks/useToast";

interface ToastViewportProps {
  toasts: Toast[];
  onDismiss?: (id: number) => void;
}

/**
 * Fixed bottom-right stack of toasts. Pair with ``useToast()``.
 * Info toasts use the card surface; errors use the destructive surface and
 * carry a close button (they linger longer because they may need action).
 */
export function ToastViewport({ toasts, onDismiss }: ToastViewportProps) {
  if (toasts.length === 0) return null;
  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((t) => {
        const isError = t.variant === "error";
        return (
          <div
            key={t.id}
            role="status"
            className={`flex items-start gap-2 rounded-lg border px-3.5 py-2.5 text-xs shadow-lg animate-in fade-in slide-in-from-bottom-2 ${
              isError
                ? "bg-destructive text-destructive-foreground border-destructive"
                : "bg-card text-foreground border-border"
            }`}
          >
            {isError ? (
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
            ) : (
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0 mt-px text-green-600" />
            )}
            <span className="flex-1 min-w-0">{t.message}</span>
            {isError && onDismiss && (
              <button
                type="button"
                onClick={() => onDismiss(t.id)}
                className="shrink-0 opacity-80 hover:opacity-100"
                aria-label="Dismiss"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
