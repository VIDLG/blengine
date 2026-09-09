import { defineConfig } from "vitest/config";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import babel from "@rolldown/plugin-babel";
import stylex from "@stylexjs/unplugin";
import { playwright } from "@vitest/browser-playwright";

export default defineConfig({
  plugins: [
    stylex.vite({ useCSSLayers: true }),
    react(),
    babel({ presets: [reactCompilerPreset()] }),
  ],
  test: {
    include: ["src/**/*.test.jsx"],
    browser: {
      enabled: true,
      provider: playwright({
        launchOptions: { headless: true },
      }),
      instances: [{ browser: "chromium" }],
    },
  },
});
