/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'sans-serif'],
      },
      colors: {
        brand: {
          primary: '#25D366',
          primaryDark: '#0F8B4C',
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
