import { defineConfig } from "vite";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import babel from "@rolldown/plugin-babel";
import stylex from "@stylexjs/unplugin";

export default defineConfig({
  plugins: [
    stylex.vite({ useCSSLayers: true }),
    react(),
    babel({
      presets: [reactCompilerPreset()],
    }),
  ],
  build: {
    target: "es2022",
  },
});
