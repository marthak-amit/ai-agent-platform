import { defineConfig } from "vite";
import { resolve } from "path";

export default defineConfig({
  build: {
    lib: {
      entry: resolve(__dirname, "src/widget.ts"),
      name: "AiAgentWidget",
      // Output filename — clients reference this as widget.min.js
      fileName: () => "widget.min",
      // IIFE = self-executing bundle, works with a plain <script> tag
      formats: ["iife"],
    },
    outDir: "dist",
    // Single file, no chunk splitting
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
    minify: true,
    sourcemap: false,
  },
});
