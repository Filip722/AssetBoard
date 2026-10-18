// Colours are theme tokens: each one reads a CSS variable that src/assetboard/static/css/input.css
// sets for the light theme (:root) and the dark theme (:root.dark). Change a colour there, once;
// templates use only these token names and never need a dark: colour variant.
const token = (name) => `rgb(var(--color-${name}) / <alpha-value>)`;

module.exports = {
  darkMode: "class",
  content: ["./src/assetboard/web/templates/**/*.html"],
  theme: {
    extend: {
      colors: {
        canvas: token("canvas"),
        surface: token("surface"),
        subtle: token("subtle"),
        field: token("field"),
        line: {
          DEFAULT: token("line"),
          strong: token("line-strong"),
        },
        ink: token("ink"),
        muted: token("muted"),
        primary: {
          DEFAULT: token("primary"),
          hover: token("primary-hover"),
          ink: token("primary-ink"),
          soft: token("primary-soft"),
        },
        "on-primary": token("on-primary"),
        accent: token("accent"),
        "on-accent": token("on-accent"),
        success: {
          DEFAULT: token("success"),
          soft: token("success-soft"),
          strong: token("success-strong"),
          line: token("success-line"),
        },
        danger: {
          DEFAULT: token("danger"),
          soft: token("danger-soft"),
          strong: token("danger-strong"),
          line: token("danger-line"),
        },
        warning: {
          soft: token("warning-soft"),
          strong: token("warning-strong"),
          line: token("warning-line"),
        },
        inverse: {
          DEFAULT: token("inverse"),
          link: token("inverse-link"),
        },
        "on-inverse": token("on-inverse"),
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      boxShadow: {
        card: "0 12px 40px rgba(0, 0, 0, 0.06)",
      },
    },
  },
  plugins: [],
};
