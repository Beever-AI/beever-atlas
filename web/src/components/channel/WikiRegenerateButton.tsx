import { useRef, useState, useEffect } from "react";
import { RefreshCw, ChevronDown, Check, Languages } from "lucide-react";

interface WikiRegenerateButtonProps {
  currentLang: string;
  supportedLanguages: string[];
  isRefreshing: boolean;
  /** Called when user clicks the main button (regenerate in currentLang). */
  onRegenerate: () => void;
  /** Called when user picks a language from the dropdown menu. */
  onRegenerateInLang: (lang: string) => void;
  /** Verb shown on the main button. "Regenerate" in header, "Generate" in empty state. */
  label?: "Regenerate" | "Generate";
  /** Size variant. "sm" fits the sidebar header; "lg" is for the empty-state CTA. */
  size?: "sm" | "lg";
}

// Human-readable names for BCP-47 tags we're likely to support.
// Falls back to the uppercased tag when unknown so we never render "undefined".
const LANG_NAMES: Record<string, string> = {
  en: "English",
  "zh-HK": "Cantonese",
  "zh-TW": "Traditional Chinese",
  "zh-CN": "Simplified Chinese",
  ja: "Japanese",
  ko: "Korean",
  es: "Spanish",
  fr: "French",
  de: "German",
  pt: "Portuguese",
  it: "Italian",
  nl: "Dutch",
  sv: "Swedish",
  da: "Danish",
  no: "Norwegian",
  fi: "Finnish",
  pl: "Polish",
  cs: "Czech",
  ru: "Russian",
  uk: "Ukrainian",
  tr: "Turkish",
  ar: "Arabic",
  he: "Hebrew",
  hi: "Hindi",
  th: "Thai",
  el: "Greek",
  vi: "Vietnamese",
  id: "Indonesian",
};

function langName(tag: string): string {
  return LANG_NAMES[tag] ?? tag.toUpperCase();
}

export function WikiRegenerateButton({
  currentLang,
  supportedLanguages,
  isRefreshing,
  onRegenerate,
  onRegenerateInLang,
  label = "Regenerate",
  size = "sm",
}: WikiRegenerateButtonProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Close dropdown on outside click.
  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  // Close dropdown on Escape.
  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open]);

  // De-duplicate while preserving order; current lang always first.
  const options = Array.from(
    new Set(
      [currentLang, ...supportedLanguages].filter(Boolean) as string[],
    ),
  );

  const isLg = size === "lg";
  const verbIng = label === "Generate" ? "Generating" : "Regenerating";
  const currentTag = currentLang.toUpperCase();

  // ---- Large (empty-state CTA) ------------------------------------------
  if (isLg) {
    return (
      <div ref={containerRef} className="relative inline-flex">
        <div className="inline-flex items-stretch rounded-full shadow-sm ring-1 ring-primary/20 transition-shadow hover:shadow-md">
          <button
            onClick={onRegenerate}
            disabled={isRefreshing}
            className="inline-flex items-center gap-2 rounded-l-full bg-primary pl-5 pr-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary/40 disabled:opacity-60"
          >
            <RefreshCw
              className={`h-4 w-4 ${isRefreshing ? "animate-spin" : ""}`}
            />
            <span>
              {isRefreshing ? `${verbIng}…` : label}
            </span>
            <span className="ml-0.5 rounded-full bg-primary-foreground/15 px-2 py-0.5 text-[11px] font-semibold tracking-wide">
              {currentTag}
            </span>
          </button>

          {options.length > 1 && (
            <>
              <div
                aria-hidden
                className="w-px self-stretch bg-primary-foreground/20"
              />
              <button
                onClick={() => setOpen((v) => !v)}
                disabled={isRefreshing}
                className="inline-flex items-center justify-center rounded-r-full bg-primary pl-3 pr-3.5 text-primary-foreground transition-colors hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary/40 disabled:opacity-60"
                aria-haspopup="true"
                aria-expanded={open}
                title={`${label} in another language`}
              >
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${
                    open ? "rotate-180" : ""
                  }`}
                />
              </button>
            </>
          )}
        </div>

        {open && (
          <LanguageMenu
            label={label}
            options={options}
            currentLang={currentLang}
            onPick={(lang) => {
              setOpen(false);
              onRegenerateInLang(lang);
            }}
            anchor="center"
          />
        )}
      </div>
    );
  }

  // ---- Small (sidebar header) -------------------------------------------
  return (
    <div ref={containerRef} className="relative inline-flex">
      <div className="inline-flex items-stretch overflow-hidden rounded-md border border-border/60 bg-muted/40">
        <button
          onClick={onRegenerate}
          disabled={isRefreshing}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-60"
          title={
            isRefreshing
              ? `${verbIng} wiki…`
              : `${label} wiki in ${langName(currentLang)}`
          }
        >
          <RefreshCw
            className={`h-3 w-3 ${isRefreshing ? "animate-spin" : ""}`}
          />
          <span>{isRefreshing ? verbIng + "…" : label}</span>
          <span className="rounded bg-background/60 px-1 py-px text-[10px] font-semibold tracking-wide text-foreground/80">
            {currentTag}
          </span>
        </button>

        {options.length > 1 && (
          <>
            <div aria-hidden className="w-px self-stretch bg-border/60" />
            <button
              onClick={() => setOpen((v) => !v)}
              disabled={isRefreshing}
              className="inline-flex items-center justify-center px-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-60"
              aria-haspopup="true"
              aria-expanded={open}
              title={`${label} in another language`}
            >
              <ChevronDown
                className={`h-3 w-3 transition-transform ${
                  open ? "rotate-180" : ""
                }`}
              />
            </button>
          </>
        )}
      </div>

      {open && (
        <LanguageMenu
          label={label}
          options={options}
          currentLang={currentLang}
          onPick={(lang) => {
            setOpen(false);
            onRegenerateInLang(lang);
          }}
          anchor="right"
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dropdown
// ---------------------------------------------------------------------------

interface LanguageMenuProps {
  label: string;
  options: string[];
  currentLang: string;
  onPick: (lang: string) => void;
  anchor: "right" | "center";
}

function LanguageMenu({
  label,
  options,
  currentLang,
  onPick,
  anchor,
}: LanguageMenuProps) {
  // "right" anchors to the right edge of the trigger (sidebar).
  // "center" centers under the trigger (empty-state CTA).
  const anchorClass =
    anchor === "right"
      ? "right-0"
      : "left-1/2 -translate-x-1/2";

  return (
    <div
      role="menu"
      className={`absolute ${anchorClass} top-full z-50 mt-2 w-64 max-h-80 overflow-y-auto rounded-xl border border-border/80 bg-popover p-1.5 shadow-lg ring-1 ring-black/5`}
    >
      <div className="flex items-center gap-1.5 px-2.5 pb-1.5 pt-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        <Languages className="h-3 w-3" />
        {label} in…
      </div>
      <div className="flex flex-col">
        {options.map((lang) => {
          const isCurrent = lang === currentLang;
          return (
            <button
              key={lang}
              role="menuitem"
              onClick={() => onPick(lang)}
              className={`group flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left text-sm transition-colors ${
                isCurrent
                  ? "bg-primary/10 text-foreground"
                  : "text-foreground hover:bg-muted"
              }`}
            >
              <span
                className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide ${
                  isCurrent
                    ? "bg-primary/20 text-primary"
                    : "bg-muted text-muted-foreground group-hover:bg-background group-hover:text-foreground"
                }`}
              >
                {lang.toUpperCase()}
              </span>
              <span className="flex-1 truncate">{langName(lang)}</span>
              {isCurrent && (
                <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
