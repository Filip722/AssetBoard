module.exports = {
  darkMode: "class",
  content: ["./src/assetboard/web/templates/**/*.html"],
  theme: {
    extend: {
      colors: {
        ink: "#172126",
        muted: "#617078",
        canvas: "#f3f6f4",
        accent: "#18705d",
        "accent-dark": "#0f5547",
        line: "#dce5e0",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      boxShadow: {
        card: "0 12px 40px rgba(26, 54, 46, 0.06)",
      },
    },
  },
  plugins: [],
};
