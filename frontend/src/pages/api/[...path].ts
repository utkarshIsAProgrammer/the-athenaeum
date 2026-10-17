import type { APIContext, APIRoute } from "astro";

const API_ORIGIN = (import.meta.env.INTERNAL_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

// What tells the API that this request came from the site rather than from
// whoever else can resolve its hostname. Compared in constant time on the far
// side; empty here simply means the API is not asking (see
// `INTERNAL_API_SECRET`), which is the case when both halves share a network.
// This is a server-only value: it never appears in the client bundle.
const API_SECRET = import.meta.env.INTERNAL_API_SECRET ?? "";

const METHODS_WITHOUT_BODY = new Set(["GET", "HEAD"]);

// A forwarded-for header is a list of hops; only the first is the client, and
// anything longer is a client trying to make this proxy hold a large header.
const MAX_FORWARDED_CHARS = 64;

/**
 * Forward `/api/*` to the FastAPI service so the browser only ever talks to
 * this origin. Keeps the API off the public network and sidesteps CORS.
 *
 * Cookies have to cross in both directions by hand: the session lives in an
 * HttpOnly cookie the browser sends to *this* origin, so dropping it here would
 * make every signed-in request arrive at the API anonymous.
 */
async function forward({ params, request, url, clientAddress }: APIContext): Promise<Response> {
  // A rest route happily matches `..`, and `new URL("/api/../../openapi.json")`
  // collapses to `/openapi.json` — a path the public origin is not supposed to
  // reach at all. Every segment has to be an ordinary one.
  const rest = params.path ?? "";
  if (rest.split("/").some((segment) => !segment || segment === "." || segment === "..")) {
    return refuse(404, "Not found.");
  }

  const target = new URL(`/api/${rest}`, `${API_ORIGIN}/`);
  // Belt as well as braces: nothing leaves here that is not under `/api/`.
  if (!target.pathname.startsWith("/api/")) return refuse(404, "Not found.");
  target.search = url.search;

  // A form posted from another site arrives with the reader's cookie attached,
  // because the cookie is sent on any request to this origin. `Origin` and
  // `Sec-Fetch-Site` are the two headers a browser will not let a page forge,
  // so either one disagreeing ends the request. Signing in and saving a story
  // are both postable, and both would be worth forging.
  //
  // Hosts are compared rather than full origins: a deployment that terminates
  // TLS in front of this server is reached over `https` while seeing `http`,
  // and a scheme mismatch would refuse every honest sign-in.
  if (!METHODS_WITHOUT_BODY.has(request.method)) {
    const site = request.headers.get("sec-fetch-site");
    if (site && site !== "same-origin" && site !== "none") {
      return refuse(403, "That request came from somewhere else.");
    }

    const origin = request.headers.get("origin");
    if (origin) {
      let host: string | null = null;
      try {
        host = new URL(origin).host;
      } catch {
        host = null;
      }
      if (host !== url.host) return refuse(403, "That request came from somewhere else.");
    }
  }

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);

  const cookie = request.headers.get("cookie");
  if (cookie) headers.set("cookie", cookie);

  // The API answers nobody else once a secret is configured, so this is what
  // makes the proxy the only way in.
  if (API_SECRET) headers.set("x-internal-secret", API_SECRET);

  // The API counts requests per client, and behind this proxy every request
  // arrives from the same socket address. Passing on who really asked is what
  // makes that count mean anything.
  const forwarded = request.headers.get("x-forwarded-for")?.split(",")[0]?.trim();
  const client = (forwarded || clientAddress || "").slice(0, MAX_FORWARDED_CHARS);
  if (client) headers.set("x-forwarded-for", client);

  const body = METHODS_WITHOUT_BODY.has(request.method) ? undefined : await request.arrayBuffer();

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body,
      redirect: "manual",
    });

    const responseHeaders = new Headers();
    const upstreamType = upstream.headers.get("content-type");
    if (upstreamType) responseHeaders.set("content-type", upstreamType);

    // Sign-in and sign-out both answer with `Set-Cookie`, and a session cookie
    // is HttpOnly — the browser only accepts it from a response it can see, so
    // this hop must hand it over.
    for (const value of upstream.headers.getSetCookie()) {
      responseHeaders.append("set-cookie", value);
    }

    const upstreamCache = upstream.headers.get("cache-control");
    responseHeaders.set("cache-control", upstreamCache ?? "no-store");

    return new Response(upstream.body, { status: upstream.status, headers: responseHeaders });
  } catch {
    return refuse(502, "The news service is unreachable.");
  }
}

/** An error in the same shape the API uses, so the client has one thing to read. */
function refuse(status: number, message: string): Response {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

export const GET: APIRoute = (context) => forward(context);
export const POST: APIRoute = (context) => forward(context);
export const PUT: APIRoute = (context) => forward(context);
export const PATCH: APIRoute = (context) => forward(context);
export const DELETE: APIRoute = (context) => forward(context);
export const OPTIONS: APIRoute = (context) => forward(context);
