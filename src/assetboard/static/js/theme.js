(() => {
  // Precedence: this device's toggle (localStorage) > the saved profile theme (data-theme on
  // <html>, rendered by the server) > the system setting. Runs in <head>, before the body paints.
  const storageKey = "theme";
  const root = document.documentElement;
  const systemDark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;
  let preference = null;
  try {
    preference = localStorage.getItem(storageKey);
  } catch {
    // Storage unavailable: fall back to the saved theme.
  }
  if (preference !== "dark" && preference !== "light") {
    const saved = root.dataset.theme;
    preference = saved === "dark" || saved === "light" ? saved : null;
  }
  root.classList.toggle("dark", preference ? preference === "dark" : systemDark());
})();
