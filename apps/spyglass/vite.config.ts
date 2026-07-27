import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [react(), tailwindcss()],

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available
  server: {
    port: 1421,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1422,
        }
      : undefined,
    watch: {
      // 3. tell Vite to ignore watching `src-tauri` and Cargo's `target`
      // build output. `target` sits next to `src-tauri`, not inside it, so
      // it needed its own entry -- left unignored, its 100k+ build
      // artifacts (11GB, rewritten heavily by every `cargo build`) kept
      // Vite's own file watcher busy enough to make even a static
      // `index.html` request take seconds, which showed up as the app
      // window opening blank and taking a while to load.
      ignored: ["**/src-tauri/**", "**/target/**"],
    },
  },
}));
