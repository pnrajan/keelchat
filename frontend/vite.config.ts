import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  base: command === "build" ? "/console/" : "/", // served by the API under /console/; Vite dev stays at /
  plugins: [react()],
  server: { port: 5173, proxy: { "/v1": "http://127.0.0.1:8000", "/health": "http://127.0.0.1:8000", "/docs": "http://127.0.0.1:8000", "/openapi.json": "http://127.0.0.1:8000" } },
  build: { outDir: "dist" },
}));
