import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production image serves the built assets from the FastAPI process, so the
// dev server proxies /api to the backend instead of enabling permissive CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://127.0.0.1:8080",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
