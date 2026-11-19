import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Vitest config for the Next.js + React + TS frontend. Mirrors the tsconfig
// "@/*" -> "./*" path alias so tests import the same way app code does.
// jsdom gives component tests a DOM. Real unit/component/E2E suites (Phase 7)
// extend this runner.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: false,
    include: ["**/__tests__/**/*.test.{ts,tsx}", "**/*.test.{ts,tsx}"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
});
