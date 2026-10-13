import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs separately (`chainwatch serve`); Vite proxies /v1 and /health.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/v1": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
    },
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
  test: { environment: "node" },
});
