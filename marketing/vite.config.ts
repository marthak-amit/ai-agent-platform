/// <reference types="vite-react-ssg/types" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5175,
    strictPort: true,
  },
  ssgOptions: {
    script: "async",
    formatting: "minify",
    entry: "src/main.tsx",
  },
});
