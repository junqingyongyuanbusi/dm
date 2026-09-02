(() => {
  const storageKey = "reply_ui_theme";
  const supportedPreferences = new Set(["system", "light", "dark"]);
  const systemThemeMedia = window.matchMedia("(prefers-color-scheme: dark)");

  const readPreference = () => {
    try {
      const storedPreference = window.localStorage.getItem(storageKey);
      return supportedPreferences.has(storedPreference)
        ? storedPreference
        : "system";
    } catch (_error) {
      return "system";
    }
  };

  const resolveTheme = (preference) => {
    if (preference === "system") {
      return systemThemeMedia.matches ? "dark" : "light";
    }
    return preference;
  };

  const applyPreference = (preference, { persist = false } = {}) => {
    const normalizedPreference = supportedPreferences.has(preference)
      ? preference
      : "system";
    const resolvedTheme = resolveTheme(normalizedPreference);

    document.documentElement.dataset.themePreference = normalizedPreference;
    document.documentElement.dataset.theme = resolvedTheme;
    document.documentElement.style.colorScheme = resolvedTheme;

    if (persist) {
      try {
        window.localStorage.setItem(storageKey, normalizedPreference);
      } catch (_error) {
        // The selected theme still applies for this page when storage is unavailable.
      }
    }

    window.dispatchEvent(
      new CustomEvent("reply-theme-change", {
        detail: { preference: normalizedPreference, theme: resolvedTheme },
      }),
    );
  };

  const getPreference = () =>
    document.documentElement.dataset.themePreference || readPreference();

  window.replyTheme = {
    applyPreference,
    getPreference,
    setPreference: (preference) =>
      applyPreference(preference, { persist: true }),
  };

  systemThemeMedia.addEventListener("change", () => {
    if (getPreference() === "system") {
      applyPreference("system");
    }
  });

  applyPreference(readPreference());
})();
