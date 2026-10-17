/** Colours come from CSS custom properties (src/styles/theme.css); nothing is hard-coded here. */
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: v("bg"),
        panel: v("panel"),
        "panel-border": v("panel-border"),
        neon: v("neon-red"),
        "neon-soft": v("neon-red-soft"),
        deep: v("deep-red"),
        text: v("text"),
        muted: v("text-muted"),
        ok: v("ok"),
      },
      fontFamily: {
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
        display: ["Orbitron", "JetBrains Mono", "sans-serif"],
      },
      screens: { xs: "480px" },
      minHeight: { touch: "44px" },
      minWidth: { touch: "44px" },
    },
  },
  plugins: [],
};
