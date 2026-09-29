import { defineConfig, mergeConfig } from "vitest/config";

import viteConfig from "./vite.config";

// Pruebas deterministas: siempre en UTC (lo heredan los procesos de Vitest).
process.env.TZ = "UTC";

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      restoreMocks: true,
      unstubGlobals: true,
      coverage: {
        provider: "v8",
        include: ["src/**/*.{ts,tsx}"],
        exclude: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/main.tsx", "src/vite-env.d.ts"],
        reporter: ["text", "html", "lcov"],
        thresholds: {
          lines: 70,
          functions: 70,
          statements: 70,
          branches: 70,
        },
      },
    },
  }),
);
