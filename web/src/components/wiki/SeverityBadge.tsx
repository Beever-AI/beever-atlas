/**
 * SeverityBadge — accessible severity indicator combining a colored
 * dot, an icon, and a screen-reader label.
 *
 * Color alone is not enough to communicate severity (color-blind
 * users + dark/light theme contrast quirks make red-vs-amber
 * indistinguishable for many readers). Pairing the dot with an icon
 * + ``aria-label`` solves both problems without changing the
 * underlying severity strings ("critical" / "high" / "medium" /
 * "low") used across the rest of the wiki.
 *
 * The component renders a single inline span; callers decide
 * placement (left-side dot, inline chip, etc.).
 */

import { Zap, TriangleAlert, Circle, CircleDot } from "lucide-react";
import type { ComponentType } from "react";

export type Severity = "critical" | "high" | "medium" | "low";

interface SeverityBadgeProps {
  severity: Severity | string | undefined | null;
  /** Visual size in pixels for the icon. Defaults to 12 (matches the
   *  inline body-text scale used in KeyFacts cards). */
  iconSize?: number;
  /** Override the default ``aria-label`` (e.g. "Critical importance"). */
  ariaLabel?: string;
  /** Additional Tailwind classes layered on top of the base
   *  inline-flex container. */
  className?: string;
  /** When false, hide the colored dot and render only the icon. Useful
   *  for tight chip rows where the dot would be visual noise. */
  showDot?: boolean;
}

interface BadgeConfig {
  dotClass: string;
  iconClass: string;
  Icon: ComponentType<{ size?: number; className?: string }>;
  label: string;
}

/** Map a severity bucket to its visual + a11y configuration. Unknown
 *  values fall through to the medium config so we never render a
 *  blank badge — the upstream data is treated as authoritative for
 *  ordering, but the visual layer is forgiving. */
function configFor(severity: string | null | undefined): BadgeConfig {
  const v = (severity || "").toLowerCase();
  if (v === "critical") {
    return {
      dotClass: "bg-red-500",
      iconClass: "text-red-500",
      Icon: Zap,
      label: "Critical importance",
    };
  }
  if (v === "high") {
    return {
      dotClass: "bg-amber-500",
      iconClass: "text-amber-500",
      Icon: TriangleAlert,
      label: "High importance",
    };
  }
  if (v === "low") {
    return {
      dotClass: "bg-muted-foreground/40",
      iconClass: "text-muted-foreground/60",
      Icon: CircleDot,
      label: "Low importance",
    };
  }
  // medium / default / unknown
  return {
    dotClass: "bg-blue-500",
    iconClass: "text-blue-500",
    Icon: Circle,
    label: "Medium importance",
  };
}

export function SeverityBadge({
  severity,
  iconSize = 12,
  ariaLabel,
  className,
  showDot = true,
}: SeverityBadgeProps) {
  const cfg = configFor(severity);
  const label = ariaLabel || cfg.label;
  return (
    <span
      role="img"
      aria-label={label}
      data-testid="severity-badge"
      data-severity={(severity || "medium").toString().toLowerCase()}
      className={
        "inline-flex items-center gap-1 flex-shrink-0 " + (className || "")
      }
    >
      {showDot && (
        <span
          aria-hidden="true"
          data-testid="severity-badge-dot"
          className={"inline-block h-1.5 w-1.5 rounded-full " + cfg.dotClass}
        />
      )}
      <cfg.Icon
        aria-hidden="true"
        size={iconSize}
        className={cfg.iconClass}
      />
    </span>
  );
}
