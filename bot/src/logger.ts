/**
 * Minimal level-gated logger.
 *
 * Reads LOG_LEVEL from the environment at module load (default "info").
 * Valid levels: "debug" | "info" | "warn" | "error" | "silent"
 *
 * No external dependencies — intentional.
 */

type Level = "debug" | "info" | "warn" | "error" | "silent";

const LEVELS: Record<Level, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
  silent: 4,
};

function currentLevel(): number {
  const raw = (process.env.LOG_LEVEL || "info").toLowerCase() as Level;
  return LEVELS[raw] ?? LEVELS.info;
}

function log(level: Level, args: unknown[]): void {
  if (LEVELS[level] < currentLevel()) return;
  const fn = level === "error" ? console.error
    : level === "warn" ? console.warn
    : level === "debug" ? console.debug
    : console.log;
  fn(...args);
}

export const logger = {
  debug: (...args: unknown[]) => log("debug", args),
  info:  (...args: unknown[]) => log("info",  args),
  warn:  (...args: unknown[]) => log("warn",  args),
  error: (...args: unknown[]) => log("error", args),
};
