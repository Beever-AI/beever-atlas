interface LanguageBadgeProps {
  lang: string | null | undefined;
  confidence?: number | null;
}

export function LanguageBadge({ lang, confidence }: LanguageBadgeProps) {
  if (!lang || lang === "en") return null;
  const pct = Math.round((confidence ?? 0) * 100);
  return (
    <span
      className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
      title={`Detected language: ${lang} (${pct}%)`}
    >
      {lang}
    </span>
  );
}
