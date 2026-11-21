import { useEffect, useRef, useState } from "react";
import { buttonClass } from "./Button";
import { applyTheme, initialTheme, saveTheme, storedTheme, systemTheme, type Theme } from "../utils/theme";

export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(
    () => (document.documentElement.dataset.theme as Theme | undefined) ?? initialTheme()
  );
  const chosen = useRef(storedTheme() !== null);

  // Follows the system setting only until the user picks a theme here.
  useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => {
      if (chosen.current) return;
      const next = systemTheme();
      applyTheme(next);
      setTheme(next);
    };
    query.addEventListener?.("change", onChange);
    return () => query.removeEventListener?.("change", onChange);
  }, []);

  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className={buttonClass("ghost", true)}
      onClick={() => {
        chosen.current = true;
        applyTheme(next);
        saveTheme(next);
        setTheme(next);
      }}
    >
      {next === "light" ? "Light theme" : "Dark theme"}
    </button>
  );
}
