import { defineMiddleware } from "astro:middleware";

/**
 * The edge every response passes through on its way out.
 *
 * Three jobs, all of which have to happen here rather than in a page:
 *
 *  * **Shape.** The security headers below are the ones a browser acts on. The
 *    API sets its own too, but the browser almost never talks to the API
 *    directly — it talks to this origin — so without them here the document
 *    would be the one thing left unfenced.
 *  * **Size.** Server renders are HTML and JSON, both of which compress by
 *    roughly 4x. That is real bandwidth on a free instance, and it is paid once
 *    per response rather than per requesting feature.
 *  * **Age.** `/api/*` is the only thing the browser caches, and which of its
 *    answers may be kept has to be decided by path rather than left to a
 *    default that would apply equally to the feed and to a session.
 */

// Answers that are the same for every reader, so a browser may keep one.
const SHARED_API = /^\/api\/(feed|search|trending|categories|articles|threads|archive|health)(\/|$)/;

const COMPRESSIBLE = /^(?:text\/|application\/(?:json|javascript|xml|manifest))/i;

function securityHeaders(url: URL): Array<[string, string]> {
  const headers: Array<[string, string]> = [
    // Nothing here loads a script, a stylesheet or a font from another origin,
    // so the only reason to allow one is exfiltration. Images do come from
    // newsrooms, which is why `img-src` is the single exception.
    //
    // `'unsafe-inline'` is still needed for scripts and styles: Astro and
    // Svelte both ship their islands as inline module bootstraps. It costs the
    // XSS half of what a CSP buys, and keeps the other half — no injection can
    // pull in a script from a host of its choosing.
    [
      "content-security-policy",
      [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "img-src 'self' data: https:",
        "font-src 'self' data:",
        "style-src 'self' 'unsafe-inline'",
        "script-src 'self' 'unsafe-inline'",
        import.meta.env.DEV ? "connect-src 'self' ws: wss:" : "connect-src 'self'",
        "manifest-src 'self'",
      ].join("; "),
    ],
    ["x-content-type-options", "nosniff"],
    ["x-frame-options", "DENY"],
    ["referrer-policy", "strict-origin-when-cross-origin"],
    ["cross-origin-opener-policy", "same-origin"],
    ["cross-origin-resource-policy", "same-origin"],
    ["permissions-policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()"],
  ];

  // Pinning a plaintext origin would lock the browser out of it, so HSTS waits
  // for the deployment to actually be served over TLS.
  if (url.protocol === "https:") {
    headers.push(["strict-transport-security", "max-age=15552000; includeSubDomains"]);
  }

  return headers;
}

function appendVary(headers: Headers, value: string): void {
  const existing = headers.get("vary");
  if (!existing) {
    headers.set("vary", value);
    return;
  }
  const seen = existing.split(",").map((part) => part.trim().toLowerCase());
  if (!seen.includes(value.toLowerCase())) headers.set("vary", `${existing}, ${value}`);
}

export const onRequest = defineMiddleware(async (context, next) => {
  const response = await next();
  const headers = new Headers(response.headers);

  for (const [name, value] of securityHeaders(context.url)) {
    headers.set(name, value);
  }

  if (context.url.pathname.startsWith("/api/")) {
    // The browser asks for feed pages of its own accord, and every one of those
    // questions was already answered a moment ago by whoever scrolled first.
    // Everything else under `/api/` is one reader's.
    const shared = context.request.method === "GET" && SHARED_API.test(context.url.pathname);
    headers.set(
      "cache-control",
      shared ? "private, max-age=30, stale-while-revalidate=120" : "no-store",
    );
  } else if (!headers.has("cache-control")) {
    // A render carries this reader's own saved count and handle. It may live in
    // their browser, and only after the server has said it is still current.
    headers.set("cache-control", "private, no-cache");
  }

  // Dev serves from memory and hot-reloads on every keystroke; compressing it
  // would only add a step between the code and the browser.
  const body = response.body;
  const wants = (context.request.headers.get("accept-encoding") ?? "").includes("gzip");
  const canCompress =
    !import.meta.env.DEV &&
    wants &&
    body !== null &&
    !headers.has("content-encoding") &&
    COMPRESSIBLE.test(headers.get("content-type") ?? "");

  if (!canCompress) {
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  }

  // The length is no longer known once the stream is wrapped, and leaving the
  // original would have the browser cut the response short.
  headers.delete("content-length");
  appendVary(headers, "accept-encoding");
  // The body is gzip from here on, and saying so is the whole difference
  // between a page and a screen of noise: without it the browser is handed
  // compressed bytes labelled `text/html`, and `nosniff` stops it guessing.
  // Compression is off in dev, so this is only ever visible once deployed.
  headers.set("content-encoding", "gzip");

  return new Response(body.pipeThrough(new CompressionStream("gzip")), {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
});
