// Offline support for The Athenaeum.
//
// The strategy is network-first for everything: the backend already does its
// own caching, and the free news tiers are limited per day, so a stale copy
// served from here would be worse than a slightly slower fresh one. The cache
// only exists to answer the question "what can I still read with no network?".
//
// `/api/` is deliberately left out of the cache. Those responses are keyed by
// query string and change constantly, so caching them would grow without bound
// and risk showing yesterday's feed as if it were today's.

const VERSION = "athenaeum-v1";

// Precached at install so the offline fallback itself never needs the network.
const SHELL = ["/offline.html", "/manifest.webmanifest", "/icon.svg"];

// Every visited page is kept so it can still be read with no network, but a
// reader who browses for a week would otherwise be storing a week of responses
// in the browser. Oldest-first eviction keeps the ceiling flat.
const MAX_ENTRIES = 60;

async function trim(cache) {
  const keys = await cache.keys();
  if (keys.length <= MAX_ENTRIES) return;
  // `keys()` is insertion-ordered, so the front of the list is the oldest.
  for (const key of keys.slice(0, keys.length - MAX_ENTRIES)) {
    await cache.delete(key);
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  event.respondWith(
    (async () => {
      const cache = await caches.open(VERSION);
      try {
        const response = await fetch(request);
        // Only complete same-origin responses are worth keeping; opaque and
        // error responses would poison the cache, and a response the server
        // marked `no-store` is one it does not want kept at all.
        const keep = response.ok && response.type === "basic" && !noStore(response);
        if (keep) {
          void cache.put(request, response.clone()).then(() => trim(cache));
        }
        return response;
      } catch {
        const cached = await cache.match(request);
        if (cached) return cached;

        if (request.mode === "navigate") {
          const offline = await cache.match("/offline.html");
          if (offline) return offline;
        }

        return new Response("Offline", {
          status: 503,
          statusText: "Offline",
          headers: { "content-type": "text/plain; charset=utf-8" },
        });
      }
    })(),
  );
});

function noStore(response) {
  // Only `no-store`. `private` means "not for a shared cache", and this cache
  // is the reader's own browser, so a personalized page is still theirs to
  // keep — which is the whole point of the offline fallback.
  return (response.headers.get("cache-control") ?? "").includes("no-store");
}
