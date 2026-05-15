import type { LucideIcon } from "lucide-react";
import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";

export interface PipelineStep {
  label: string;
  icon: LucideIcon;
  done: boolean;
  active: boolean;
}

interface PipelineEmptyStateProps {
  icon: LucideIcon;
  title: string;
  description: string;
  steps: PipelineStep[];
  primaryActionLabel?: string;
  onPrimaryAction?: () => void;
  primaryActionDisabled?: boolean;
  primaryActionLoading?: boolean;
  secondaryActionLabel?: string;
  onSecondaryAction?: () => void;
  secondaryActionVariant?: "button" | "link";
  helperText?: string;
  children?: React.ReactNode;
}

export function PipelineEmptyState({
  icon: Icon,
  title,
  description,
  steps,
  primaryActionLabel,
  onPrimaryAction,
  primaryActionDisabled = false,
  primaryActionLoading = false,
  secondaryActionLabel,
  onSecondaryAction,
  secondaryActionVariant = "button",
  helperText,
  children,
}: PipelineEmptyStateProps) {
  return (
    <div className="flex h-full min-h-0 items-center justify-center px-6 py-12">
      <div className="mx-auto w-full max-w-xl motion-safe:animate-rise-in">
        {/* Hero icon with gradient halo. Pulses when any step is "active"
         *  so users immediately see that work is happening. */}
        {(() => {
          const hasActive = steps.some((s) => s.active);
          return (
            <div className="relative mx-auto mb-6 flex h-16 w-16 items-center justify-center motion-safe:animate-scale-in">
              <div
                className={
                  hasActive
                    ? "absolute inset-0 rounded-2xl bg-gradient-to-br from-primary/30 to-primary/10 blur-xl motion-safe:animate-pulse"
                    : "absolute inset-0 rounded-2xl bg-gradient-to-br from-primary/20 to-primary/5 blur-xl"
                }
              />
              <div className="relative flex h-16 w-16 items-center justify-center rounded-2xl border border-primary/20 bg-gradient-to-br from-primary/15 to-primary/5 shadow-sm">
                <Icon className={`h-7 w-7 text-primary ${hasActive ? "motion-safe:animate-pulse" : ""}`} />
              </div>
            </div>
          );
        })()}

        {/* Title + description */}
        <h3 className="text-center text-xl font-semibold tracking-tight text-foreground">
          {title}
        </h3>
        <p className="mx-auto mt-2 max-w-md text-center text-sm leading-relaxed text-muted-foreground">
          {description}
        </p>

        {/* Horizontal stepper */}
        <div className="mx-auto mt-8 w-full max-w-md">
          <div className="relative flex items-start justify-between">
            {/* Connector line (behind circles) */}
            <div className="absolute left-0 right-0 top-5 h-0.5 bg-border" aria-hidden />
            <div
              className="absolute left-0 top-5 h-0.5 bg-emerald-500/60 transition-all"
              style={{
                width: `${
                  steps.length <= 1
                    ? 0
                    : (steps.filter((s) => s.done).length /
                        (steps.length - 1)) *
                      100
                }%`,
              }}
              aria-hidden
            />
            {/* Active-step shimmer — slides across the full connector when
             *  any step is active. Visual signal that the pipeline is
             *  doing work, not just frozen. */}
            {steps.some((s) => s.active) && (
              <div
                className="absolute left-0 right-0 top-5 h-0.5 overflow-hidden"
                aria-hidden
              >
                <span className="absolute inset-0 bg-gradient-to-r from-transparent via-primary/60 to-transparent motion-safe:animate-stepper-shimmer" />
              </div>
            )}

            {steps.map((step, idx) => {
              const StepIcon = step.icon;
              const state = step.done ? "done" : step.active ? "active" : "pending";
              return (
                <div
                  key={step.label}
                  className="relative z-10 flex flex-1 flex-col items-center gap-2 motion-safe:animate-fade-in"
                  style={{ animationDelay: `${Math.min(idx, 5) * 70}ms` }}
                >
                  <div className="relative">
                    {state === "active" && (
                      <span
                        aria-hidden
                        className="absolute inset-0 rounded-full bg-primary/30 motion-safe:animate-ping"
                        style={{ animationDuration: "1.8s" }}
                      />
                    )}
                    <div
                      className={`relative flex h-10 w-10 items-center justify-center rounded-full border-2 transition-all duration-300 ease-out ${
                        state === "done"
                          ? "border-emerald-500 bg-emerald-500 text-white shadow-sm"
                          : state === "active"
                            ? "border-primary bg-card text-primary shadow-[0_0_0_4px_hsl(var(--primary)/0.15)] motion-safe:animate-pulse"
                            : "border-border bg-card text-muted-foreground/50"
                      }`}
                    >
                      {state === "done" ? (
                        <Check className="h-4 w-4" strokeWidth={3} />
                      ) : (
                        <StepIcon className="h-4 w-4" />
                      )}
                    </div>
                  </div>
                  <div className="flex flex-col items-center gap-0.5 text-center">
                    <span
                      className={`text-[10px] font-semibold uppercase tracking-wider ${
                        state === "active"
                          ? "text-primary"
                          : state === "done"
                            ? "text-emerald-600 dark:text-emerald-500"
                            : "text-muted-foreground/60"
                      }`}
                    >
                      Step {idx + 1}
                    </span>
                    <span
                      className={`text-xs font-medium ${
                        state === "pending"
                          ? "text-muted-foreground"
                          : "text-foreground"
                      }`}
                    >
                      {step.label}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {(primaryActionLabel || secondaryActionLabel || helperText || children) && (
          <div className="mt-8 flex flex-col items-center gap-3">
            {primaryActionLabel && onPrimaryAction && (
              <Button
                size="lg"
                onClick={onPrimaryAction}
                disabled={primaryActionDisabled || primaryActionLoading}
                className={`px-5 transition-all duration-200 ${primaryActionLoading ? "motion-safe:animate-pulse" : ""}`}
              >
                {primaryActionLoading ? "Syncing..." : primaryActionLabel}
              </Button>
            )}
            {secondaryActionLabel && onSecondaryAction && (
              secondaryActionVariant === "link" ? (
                <button
                  type="button"
                  onClick={onSecondaryAction}
                  className="text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  {secondaryActionLabel}
                </button>
              ) : (
                <Button variant="outline" size="sm" onClick={onSecondaryAction}>
                  {secondaryActionLabel}
                </Button>
              )
            )}
            {helperText && (
              <p className="text-xs text-muted-foreground">{helperText}</p>
            )}
            {children}
          </div>
        )}
      </div>
    </div>
  );
}
