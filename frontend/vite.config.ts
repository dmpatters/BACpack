/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Builds straight into the Python package (src/bacpack/web/spa) rather than
// a top-level frontend/dist -- the build output is committed to git and
// shipped as package data, so there's no separate copy step to keep in sync.
export default defineConfig({
  plugins: [tailwindcss(), react()],
  build: {
    outDir: "../src/bacpack/web/spa",
    emptyOutDir: true,
  },
  server: {
    // Forwards API calls (and the OpenAPI document) to a locally-running `uvicorn` backend during
    // `npm run dev`. Production never runs this dev server.
    proxy: {
      "/api": "http://localhost:8000",
      "/openapi.json": "http://localhost:8000",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/setupTests.ts"],
    globals: true,
  },
});
