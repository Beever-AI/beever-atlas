import { useRef, useState, useEffect } from "react";
import { RefreshCw, ChevronDown, Check, Languages } from "lucide-react";
import { wikiT } from "@/lib/wikiI18n";

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
  /** Size variant. "sm" for compact toolbar use, "md" for primary sidebar action,
   *  "lg" for the empty-state CTA. */
  size?: "sm" | "md" | "lg";
  /** Stretch to the parent container's full width (sidebar bottom primary action). */
  fullWidth?: boolean;
  /** BCP-47 tag for UI label localization. Defaults to currentLang. */
  lang?: string;
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
  fullWidth = false,
  lang,
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
  const isMd = size === "md";
  const uiLang = lang ?? currentLang;
  const labelText = label === "Generate" ? wikiT(uiLang, "generate") : wikiT(uiLang, "regenerate");
  const verbIng = label === "Generate" ? wikiT(uiLang, "generating") : wikiT(uiLang, "regenerating");
  const menuHeader = label === "Generate" ? wikiT(uiLang, "generateInLabel") : wikiT(uiLang, "regenerateInLabel");
  const anotherLangTooltip = wikiT(uiLang, "anotherLanguage");
  const currentTag = currentLang.toUpperCase();

  // ---- Medium (primary sidebar action) ----------------------------------
  if (isMd) {
    return (
      <div
        ref={containerRef}
        className={`relative ${fullWidth ? "flex" : "inline-flex"}`}
      >
        <div
          className={`${
            fullWidth ? "flex w-full" : "inline-flex"
          } items-stretch overflow-hidden rounded-lg bg-primary shadow-sm ring-1 ring-primary/30 transition-shadow hover:shadow-md`}
        >
          <button
            onClick={onRegenerate}
            disabled={isRefreshing}
            className="flex flex-1 items-center justify-center gap-2 px-3 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary/40 disabled:opacity-60"
            title={
              isRefreshing
                ? `${verbIng} wiki…`
                : `${labelText} wiki in ${langName(currentLang)}`
            }
          >
            <RefreshCw
              className={`h-4 w-4 ${isRefreshing ? "animate-spin" : ""}`}
            />
            <span>{isRefreshing ? verbIng + "…" : labelText}</span>
            <span className="rounded bg-primary-foreground/15 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide">
              {currentTag}
            </span>
          </button>

          {options.length > 1 && (
            <>
              <div
                aria-hidden
                className="w-px self-stretch bg-primary-foreground/25"
              />
              <button
                onClick={() => setOpen((v) => !v)}
                disabled={isRefreshing}
                className="flex items-center justify-center px-2.5 text-primary-foreground transition-colors hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary/40 disabled:opacity-60"
                aria-haspopup="true"
                aria-expanded={open}
                title={anotherLangTooltip}
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
            menuHeader={menuHeader}
            options={options}
            currentLang={currentLang}
            onPick={(lang) => {
              setOpen(false);
              onRegenerateInLang(lang);
            }}
            placement="top"
          />
        )}
      </div>
    );
  }

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
              {isRefreshing ? `${verbIng}…` : labelText}
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
                title={anotherLangTooltip}
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
            menuHeader={menuHeader}
            options={options}
            currentLang={currentLang}
            onPick={(lang) => {
              setOpen(false);
              onRegenerateInLang(lang);
            }}
            placement="top"
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
              : `${labelText} wiki in ${langName(currentLang)}`
          }
        >
          <RefreshCw
            className={`h-3 w-3 ${isRefreshing ? "animate-spin" : ""}`}
          />
          <span>{isRefreshing ? verbIng + "…" : labelText}</span>
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
              title={anotherLangTooltip}
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
          menuHeader={menuHeader}
          options={options}
          currentLang={currentLang}
          onPick={(lang) => {
            setOpen(false);
            onRegenerateInLang(lang);
          }}
          placement="bottom"
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dropdown
// ---------------------------------------------------------------------------

interface LanguageMenuProps {
  menuHeader: string;
  options: string[];
  currentLang: string;
  onPick: (lang: string) => void;
  placement: "top" | "bottom";
}

function LanguageMenu({
  menuHeader,
  options,
  currentLang,
  onPick,
  placement,
}: LanguageMenuProps) {
  // Always right-anchored so the menu grows leftward from the chevron —
  // avoids off-center clashes and right-edge bleed. "top" opens upward
  // (above the button), "bottom" opens downward.
  const placementClass =
    placement === "top"
      ? "bottom-full mb-2"
      : "top-full mt-2";

  return (
    <div
      role="menu"
      className={`absolute left-0 ${placementClass} z-50 w-60 max-h-80 overflow-y-auto rounded-xl border border-border bg-popover p-1.5 shadow-xl`}
    >
      <div className="flex items-center gap-1.5 px-2.5 pb-1.5 pt-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        <Languages className="h-3 w-3" />
        {menuHeader}
      </div>
      <div className="flex flex-col">
        {options.map((lang) => {
          const isCurrent = lang === currentLang;
          return (
            <button
              key={lang}
              role="menuitem"
              onClick={() => onPick(lang)}
              className={`group flex items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm transition-colors ${
                isCurrent
                  ? "bg-primary/10 text-foreground"
                  : "text-foreground hover:bg-muted"
              }`}
            >
              <span className="flex-1 truncate font-medium">
                {langName(lang)}
              </span>
              <span
                className={`shrink-0 font-mono text-[10px] tracking-wide ${
                  isCurrent ? "text-primary" : "text-muted-foreground"
                }`}
              >
                {lang.toUpperCase()}
              </span>
              {isCurrent ? (
                <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
              ) : (
                <span aria-hidden className="h-3.5 w-3.5 shrink-0" />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
