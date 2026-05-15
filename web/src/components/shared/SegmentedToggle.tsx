/**
 * SegmentedToggle — iOS-style segmented control for "view" sub-navigation.
 *
 * Used as the secondary tab pattern inside a top-level tab (e.g. inside
 * Channel Wiki to flip between Pages and Graph, or inside Agent Memory
 * to flip between Memory and Graph). The active option sits on a raised
 * "tab" background; inactive ones are quiet text.
 *
 * Visual hierarchy:
 *   - Top tabs (channel-level): text + bottom indicator, brightest.
 *   - SegmentedToggle (view-level): pill background + raised active —
 *     loud enough to discover at a glance, quiet enough not to compete
 *     with the primary nav.
 */
import { type ComponentType, type SVGProps } from "react";

export type SegmentedOption<TValue extends string> = {
  value: TValue;
  label: string;
  icon?: ComponentType<SVGProps<SVGSVGElement>>;
  testId?: string;
};

interface SegmentedToggleProps<TValue extends string> {
  ariaLabel: string;
  value: TValue;
  options: ReadonlyArray<SegmentedOption<TValue>>;
  onChange: (next: TValue) => void;
  className?: string;
}

export function SegmentedToggle<TValue extends string>({
  ariaLabel,
  value,
  options,
  onChange,
  className = "",
}: SegmentedToggleProps<TValue>) {
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={
        "inline-flex items-center gap-0.5 rounded-lg bg-muted/50 p-1 " +
        "shadow-inner shadow-black/5 dark:shadow-white/5 " +
        className
      }
    >
      {options.map((opt) => {
        const isActive = opt.value === value;
        const Icon = opt.icon;
        return (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={isActive}
            onClick={() => onChange(opt.value)}
            data-testid={opt.testId}
            className={
              "inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 " +
              "text-sm font-medium transition-all duration-150 " +
              (isActive
                ? "bg-background text-foreground shadow-sm ring-1 ring-border/40"
                : "text-muted-foreground hover:text-foreground hover:bg-background/40")
            }
          >
            {Icon && <Icon className="h-4 w-4 shrink-0" />}
            <span>{opt.label}</span>
          </button>
        );
      })}
    </div>
  );
}

export default SegmentedToggle;
