import type { LucideIcon } from "lucide-react";
import { Check } from "lucide-react";

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
  children?: React.ReactNode;
}

export function PipelineEmptyState({
  icon: Icon,
  title,
  description,
  steps,
  children,
}: PipelineEmptyStateProps) {
  return (
    <div className="flex h-full min-h-0 items-center justify-center px-6 py-12">
      <div className="mx-auto w-full max-w-xl">
        {/* Hero icon with gradient halo */}
        <div className="relative mx-auto mb-6 flex h-16 w-16 items-center justify-center">
          <div className="absolute inset-0 rounded-2xl bg-gradient-to-br from-primary/20 to-primary/5 blur-xl" />
          <div className="relative flex h-16 w-16 items-center justify-center rounded-2xl border border-primary/20 bg-gradient-to-br from-primary/15 to-primary/5 shadow-sm">
            <Icon className="h-7 w-7 text-primary" />
          </div>
        </div>

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

            {steps.map((step, idx) => {
              const StepIcon = step.icon;
              const state = step.done ? "done" : step.active ? "active" : "pending";
              return (
                <div
                  key={step.label}
                  className="relative z-10 flex flex-1 flex-col items-center gap-2"
                >
                  <div
                    className={`flex h-10 w-10 items-center justify-center rounded-full border-2 transition-all ${
                      state === "done"
                        ? "border-emerald-500 bg-emerald-500 text-white shadow-sm"
                        : state === "active"
                          ? "border-primary bg-card text-primary shadow-[0_0_0_4px_hsl(var(--primary)/0.1)]"
                          : "border-border bg-card text-muted-foreground/50"
                    }`}
                  >
                    {state === "done" ? (
                      <Check className="h-4 w-4" strokeWidth={3} />
                    ) : (
                      <StepIcon className="h-4 w-4" />
                    )}
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

        {children && (
          <div className="mt-8 flex flex-col items-center gap-3">{children}</div>
        )}
      </div>
    </div>
  );
}
