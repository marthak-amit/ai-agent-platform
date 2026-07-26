/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "sans-serif"],
      },
      colors: {
        brand: {
          primary: '#25D366',
          // Darkened from #0F8B4C: at #0F8B4C, primaryDark-on-white text (used
          // for every section eyebrow label and text link) measures 4.36:1,
          // just under WCAG AA's 4.5:1 for normal-size text. #0C6F3D clears
          // 4.5:1 in all three places this token is used as text: on white,
          // on the pale brand-primary/10 badge background, and as white text
          // on a primaryDark button background.
          primaryDark: '#0C6F3D',
          secondary: '#1A2E44',
          bg: '#F7F9F8',
          bgDark: '#1F2937',
          accent: '#F2536B',
          warning: '#F5A623',
        },
      },
    },
  },
  plugins: [],
};
