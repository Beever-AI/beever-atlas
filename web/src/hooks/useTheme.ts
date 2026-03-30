import { useEffect, useState } from "react";

type Theme = "light" | "dark" | "system";
const THEME_KEY = "theme";
const THEME_EVENT = "beever-theme-change";

function getSystemTheme(): "light" | "dark" {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function readStoredTheme(): Theme {
  const stored = localStorage.getItem(THEME_KEY) as Theme | null;
  return stored ?? "system";
}

function applyTheme(theme: Theme) {
  if (theme === "dark") {
    document.documentElement.classList.add("dark");
  } else {
    document.documentElement.classList.remove("dark");
  }
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(() => readStoredTheme());
  const [systemTheme, setSystemTheme] = useState<"light" | "dark">(() => getSystemTheme());
  const resolvedTheme = theme === "system" ? systemTheme : theme;

  useEffect(() => {
    applyTheme(resolvedTheme);
  }, [resolvedTheme]);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = (event: MediaQueryListEvent) => {
      setSystemTheme(event.matches ? "dark" : "light");
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  useEffect(() => {
    const syncTheme = () => setThemeState(readStoredTheme());
    const storageHandler = (event: StorageEvent) => {
      if (event.key === THEME_KEY) syncTheme();
    };

    window.addEventListener("storage", storageHandler);
    window.addEventListener(THEME_EVENT, syncTheme);
    return () => {
      window.removeEventListener("storage", storageHandler);
      window.removeEventListener(THEME_EVENT, syncTheme);
    };
  }, []);

  function setTheme(next: Theme) {
    localStorage.setItem(THEME_KEY, next);
    setThemeState(next);
    window.dispatchEvent(new Event(THEME_EVENT));
  }

  function toggleTheme() {
    setTheme(resolvedTheme === "dark" ? "light" : "dark");
  }

  return { theme, resolvedTheme, setTheme, toggleTheme };
}
