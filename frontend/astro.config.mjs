import node from "@astrojs/node";
import svelte from "@astrojs/svelte";
import { defineConfig } from "astro/config";

// Server-rendered so the feed arrives as real HTML instead of a loading
// skeleton, and so the browser never has to talk to the FastAPI service
// cross-origin — the catch-all route in `src/pages/api/[...path].ts` forwards.
export default defineConfig({
  output: "server",
  adapter: node({ mode: "standalone" }),
  integrations: [svelte()],
  server: { host: true, port: 4321 },
  devToolbar: { enabled: false },
});
