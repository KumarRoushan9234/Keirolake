/** @type {import('tailwindcss').Config} */
// Design tokens: a Docker Desktop–style resource manager. One blue accent, neutral grey/navy surfaces,
// status colours only for state. Surfaces, lines, text and accent tints are CSS variables so the dark theme
// can swap them; solid accent steps are fixed because they carry white text in both themes.
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;

module.exports = {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        accent: {
          50: v("accent-50"), 100: v("accent-100"), 200: v("accent-200"), 300: "#7da8ff",
          400: v("accent-400"), 500: v("accent-500"), 600: v("accent-600"), 700: "#0d3f9e", 800: v("accent-800"), 900: "#082862",
        },
        coral: { 50: v("coral-50"), 100: v("coral-100"), 200: v("coral-200"), 400: "#e5484d", 500: v("coral-500") },
        sage: { 50: v("sage-50"), 100: v("sage-100"), 200: v("sage-200"), 400: "#2fa36b", 500: v("sage-500") },
        cream: { 100: v("cream-100"), 500: v("cream-500") },
        surface: { primary: v("surface-primary"), secondary: v("surface-secondary"), tertiary: v("surface-tertiary") },
        line: v("line"),
        text: { primary: v("text-primary"), secondary: v("text-secondary"), muted: v("text-muted") },
      },
      fontFamily: {
        sans: ['"Geist Variable"', "Geist", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ['"Geist Mono Variable"', "ui-monospace", "SFMono-Regular", "Consolas", "monospace"],
      },
      transitionTimingFunction: { DEFAULT: "cubic-bezier(0.22, 1, 0.36, 1)", out: "cubic-bezier(0.22, 1, 0.36, 1)" },
      transitionDuration: { DEFAULT: "150ms" },
      keyframes: {
        "fade-up": { from: { opacity: "0", transform: "translateY(6px)" }, to: { opacity: "1", transform: "translateY(0)" } },
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        pulse: { "0%, 100%": { opacity: "1" }, "50%": { opacity: "0.45" } },
      },
      animation: {
        "fade-up": "fade-up 200ms cubic-bezier(0.22, 1, 0.36, 1) both",
        "fade-in": "fade-in 150ms cubic-bezier(0.22, 1, 0.36, 1) both",
        skeleton: "pulse 1600ms cubic-bezier(0.22, 1, 0.36, 1) infinite",
      },
    },
  },
  plugins: [],
};
