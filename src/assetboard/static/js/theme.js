(() => {
  const storageKey = "theme";
  try {
    const preference = localStorage.getItem(storageKey);
    const dark = preference ? preference === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.classList.toggle("dark", dark);
  } catch {
    document.documentElement.classList.toggle("dark", window.matchMedia("(prefers-color-scheme: dark)").matches);
  }
})();
