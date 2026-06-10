import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// VITE_BACKEND_URL is a Node-level env var read by the dev server's proxy,
// not a browser var. In Docker it is set to http://backend:8000 so requests
// from the Vite dev server reach the backend container by service name.
// Locally (no Docker) it falls back to localhost:8000.
const backendUrl = process.env.VITE_BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: backendUrl,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
