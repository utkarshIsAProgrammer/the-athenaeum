import node from "@astrojs/node";
import svelte from "@astrojs/svelte";
import vercel from "@astrojs/vercel";
import { defineConfig } from "astro/config";

// Two hosts, one codebase. Vercel serves serverless functions, which the
// standalone Node adapter cannot produce; anywhere else — this machine, or a
// container — it is the other way round. `VERCEL` is set by the platform
// itself during its builds, so the choice needs no configuration.
const adapter = process.env.VERCEL ? vercel() : node({ mode: "standalone" });

// Server-rendered so the feed arrives as real HTML instead of a loading
// skeleton, and so the browser never has to talk to the FastAPI service
// cross-origin — the catch-all route in `src/pages/api/[...path].ts` forwards.
export default defineConfig({
  output: "server",
  adapter,
  integrations: [svelte()],
  server: { host: true, port: 4321 },
  devToolbar: { enabled: false },
});
