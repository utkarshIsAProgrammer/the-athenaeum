"""Guards that sit in front of every route.

Two concerns, both of which have to be answered before a request reaches a
route rather than inside one:

  * **Cost.** The scarce resource here is not CPU, it is the three free news
    tiers' daily allowances. A single loop hammering `/api/feed` can spend a
    day's quota in minutes, so requests are counted per client before anything
    else happens. This is not accounting — it is a stop for a runaway client.
  * **Shape.** Responses carry a fixed set of headers, and a request body is
    never buffered past a hard ceiling, so neither a leaked header nor an
    enormous POST can be used against the instance.

Both are pure ASGI rather than `BaseHTTPMiddleware`, which would buffer every
response body to rewrite headers it does not need to touch.
"""

from __future__ import annotations

import json
import time
from collections import deque

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings

# Applied to every response this service emits. The browser normally talks to
# the Astro proxy rather than here, so these are the second line of the same
# defence the proxy mounts at the edge — and the first for a direct caller.
#
# `default-src 'none'` is correct for an API: nothing here is a document, and
# the one response that is (the OpenAPI page) is not reachable through the
# public origin, which only forwards `/api/`.
_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "strict-origin-when-cross-origin"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-site"),
    ("permissions-policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    ("content-security-policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"),
)

# Only sent once the operator has said the site is served over HTTPS: pinning a
# plaintext development origin would lock the browser out of it for a fortnight.
_HSTS = "max-age=15552000; includeSubDomains"

# Paths the platform's own prober uses. Refusing one of these gets the instance
# recycled, so they are never counted.
_HEALTH_SUFFIX = "/health"

_AUTH_PREFIX = "/api/auth/"


class SlidingWindow:
    """A per-key request counter over a rolling window.

    In-process, which is the right shape here: the app runs as one instance, so
    there is no second process to agree with. Memory is held to `max_keys`: a
    sweep drops the keys that have gone quiet, and if that is not enough — a
    client rotating its address faster than the window turns over — the
    quietest key is evicted to make room. Without that second step the table is
    only bounded by how many addresses one attacker can invent.
    """

    def __init__(self, limit: int, window_seconds: float, *, max_keys: int = 4096) -> None:
        self._limit = max(1, limit)
        self._window = max(1.0, window_seconds)
        self._max_keys = max(1, max_keys)
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        cutoff = moment - self._window

        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self._max_keys:
                self._sweep(cutoff)
            if len(self._hits) >= self._max_keys:
                self._evict_quietest()
            hits = self._hits[key] = deque()

        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self._limit:
            return False

        hits.append(moment)
        return True

    def _sweep(self, cutoff: float) -> None:
        """Drop keys with nothing left inside the window."""
        self._hits = {key: hits for key, hits in self._hits.items() if hits and hits[-1] > cutoff}

    def _evict_quietest(self) -> None:
        quietest = min(self._hits, key=lambda key: self._hits[key][-1])
        del self._hits[quietest]


def _header(scope: Scope, name: bytes) -> bytes | None:
    for header, value in scope.get("headers") or ():
        if header == name:
            return value
    return None


def _capped_receive(receive: Receive, limit: int) -> Receive:
    """Wrap `receive` so no more than `limit` bytes are ever held at once.

    A `content-length` check alone is not enough — a chunked body carries no
    length — so the stream itself is capped. What is handed on past the limit
    is truncated rather than refused: the JSON that follows is invalid, which
    the routers already answer with a 422, and nothing unbounded is ever
    buffered.
    """

    seen = 0

    async def capped() -> Message:
        nonlocal seen
        message = await receive()
        if message.get("type") != "http.request":
            return message

        body = message.get("body", b"")
        seen += len(body)
        if seen <= limit:
            return message

        keep = max(0, len(body) - (seen - limit))
        return {**message, "body": body[:keep]}

    return capped


class GuardsMiddleware:
    """Counts requests per client and bounds the size of a request body."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._general = SlidingWindow(settings.rate_limit_requests, settings.rate_limit_window_seconds)
        self._auth = SlidingWindow(
            settings.rate_limit_auth_requests, settings.rate_limit_auth_window_seconds
        )
        self._max_bytes = max(1024, settings.max_request_bytes)
        self._trust_forwarded = settings.trust_forwarded_for

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # A CORS preflight is not the client doing anything, and a failed health
        # check is the platform deciding whether to keep this instance.
        if method == "OPTIONS" or path.endswith(_HEALTH_SUFFIX):
            await self.app(scope, receive, send)
            return

        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_bytes:
                    await _refuse(send, 413, "That request is larger than this service accepts.")
                    return
            except ValueError:
                await _refuse(send, 400, "Invalid Content-Length.")
                return

        limiter = self._auth if path.startswith(_AUTH_PREFIX) else self._general
        if not limiter.allow(self._client(scope)):
            await _refuse(send, 429, "Too many requests. Wait a moment and try again.")
            return

        await self.app(scope, _capped_receive(receive, self._max_bytes), send)

    def _client(self, scope: Scope) -> str:
        """Who to count this against.

        The real client is behind the Astro proxy, so the forwarded address is
        the only useful key. Trusting it would be a bypass if this service were
        public — it is not, it listens on loopback and the proxy writes the
        header, which is what `trust_forwarded_for` documents.
        """
        if self._trust_forwarded:
            forwarded = _header(scope, b"x-forwarded-for")
            if forwarded:
                first = forwarded.decode("latin-1").split(",")[0].strip()
                if first:
                    return first

        client = scope.get("client")
        return client[0] if client else "unknown"


class SecurityHeadersMiddleware:
    """Puts the fixed header set on every response, including refusals."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self._hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _HEADERS:
                    headers.setdefault(name, value)
                if self._hsts:
                    headers.setdefault("strict-transport-security", _HSTS)
            await send(message)

        await self.app(scope, receive, with_headers)


async def _refuse(send: Send, status: int, message: str) -> None:
    """Answer without routing, in the same `{"error": ...}` envelope as the API."""
    from .errors import error_payload

    body = json.dumps(error_payload(message)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
