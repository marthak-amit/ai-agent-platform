/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "sans-serif"],
      },
      // No custom color palette: mirrors frontend/tailwind.config.js, which
      // uses Tailwind's stock palette (indigo primary, gray neutrals) directly
      // in components rather than custom tokens. Keep these configs in sync.
    },
  },
  plugins: [],
};
