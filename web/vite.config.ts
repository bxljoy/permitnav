import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the API runs on :8000 (uvicorn); the built app is served by the API itself.
const api = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/ask": api, "/source": api, "/auth-check": api, "/health": api } },
  build: { outDir: "dist", sourcemap: false },
});
