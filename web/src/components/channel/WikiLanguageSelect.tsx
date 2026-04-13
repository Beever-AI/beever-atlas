interface WikiLanguageSelectProps {
  channelId: string;
  primaryLanguage: string | null | undefined;
  defaultTargetLanguage: string;
  currentTargetLang: string;
  onChange: (lang: string) => void;
}

export function WikiLanguageSelect({
  channelId,
  primaryLanguage,
  defaultTargetLanguage,
  currentTargetLang,
  onChange,
}: WikiLanguageSelectProps) {
  const options = Array.from(
    new Set([primaryLanguage, defaultTargetLanguage].filter(Boolean) as string[])
  );

  if (options.length <= 1) return null;

  function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const value = e.target.value;
    try {
      localStorage.setItem(`wiki.targetLang.${channelId}`, value);
    } catch {
      // Silently ignore — private-mode Safari throws on localStorage access
    }
    onChange(value);
  }

  return (
    <select
      value={currentTargetLang}
      onChange={handleChange}
      className="rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
    >
      {options.map((lang) => (
        <option key={lang} value={lang}>
          {lang.toUpperCase()}
        </option>
      ))}
    </select>
  );
}
