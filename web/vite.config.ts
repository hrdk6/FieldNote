import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built UI ships inside the Python package so `fieldnote dashboard` serves it with the API.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/fieldnote/web/static", emptyOutDir: true, chunkSizeWarningLimit: 900 },
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8502" } },
});
