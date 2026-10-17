"""Recovering a story's own words when no provider will hand them over.

Two of the three free tiers never return a body: NewsAPI truncates at ~200
characters and NewsData answers with the literal string "ONLY AVAILABLE IN PAID
PLANS". A summary written from a headline and a standfirst is not a summary, it
is a restatement — which is exactly the dead end a reader hits when the app
tells them there is "not enough text to summarise".

So when a story is opened, and only then, the page the card points at is read
and its main text is kept. That costs no provider quota, is paid once per
article ever, and is the difference between a summary and a shrug.

The URL is third-party input and is treated as such: only public `http(s)`
hosts are fetched, redirects are followed by hand so every hop is checked
again, the response is size-capped, and the extraction is stdlib-only so no
parser becomes another thing to audit.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from .quality import ALWAYS_BLOCKED_HOSTS, matches_host

log = logging.getLogger(__name__)

# A newsroom's homepage is not a story. The read is stopped well short of
# anything that would make this a crawler.
DEFAULT_MAX_BYTES = 400_000
DEFAULT_MAX_CHARS = 20_000
DEFAULT_MIN_CHARS = 200

# Sites' own boilerplate, which survives every extractor. Anchored so a
# sentence that merely mentions one of these words is left alone.
_BOILERPLATE = re.compile(
    r"^(advertisement|sign ?up|subscribe|log ?in|sign ?in|share (this|on)|"
    r"read more|related (stories|articles|coverage)|most read|skip to|"
    r"cookie (policy|settings)|accept (all )?cookies|privacy policy|"
    r"terms (of service|and conditions)|comments?|loading|menu|search)\b",
    re.IGNORECASE,
)

# Tags whose contents are never article prose.
_SKIP_TAGS = frozenset(
    {
        "head", "script", "style", "noscript", "template", "svg", "canvas",
        "nav", "header", "footer", "aside", "form", "button", "select",
        "option", "iframe", "video", "audio", "picture", "figure",
    }
)

# Tags that end a paragraph.
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "main", "li", "ul", "ol", "tr", "td",
        "th", "dd", "dt", "blockquote", "pre", "br", "figcaption",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }
)

# The two containers a newsroom uses for the story itself, as opposed to the
# page around it.
_ARTICLE_TAGS = frozenset({"article", "main"})

MIN_PARAGRAPH_CHARS = 25
# Below this the "main container" guess is not worth trusting over the whole
# page, which happens on sites that put a summary card inside a `<main>`.
MIN_ARTICLE_CHARS = 400

# A redirect chain longer than this is a loop or a trap.
MAX_REDIRECTS = 3

# What the publisher sees. Not a disguise: it is the string a browser sends, and
# sites demonstrably behave differently without it (see `_hop`).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# The well-known NAT64 prefix. An IPv6 address inside it is not really an IPv6
# host: a carrier-grade NAT gateway forwards it to the IPv4 address in the last
# four bytes. Half the web has one of these beside its real A records — including
# `www.bbc.com` — and judging the wrapper instead of the destination is how an
# ordinary newsroom gets refused.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def _unwrap(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Any:
    """The address a connection would really reach.

    Only the `/96` form is unwrapped, because only there is the embedded IPv4
    unambiguously the last four bytes. Unwrapping rather than blanket-allowing
    is what keeps this safe: `64:ff9b::a00:1` is 10.0.0.1 and is still refused.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _host_is_routable(host: str) -> bool:
    """Resolve `host` and refuse it unless every address is public.

    Checked before the request rather than after: the point is to never open a
    socket to the metadata service or to whatever else is listening on the
    private network.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False

    if not infos:
        return False

    for info in infos:
        address = info[4][0]
        try:
            ip = _unwrap(ipaddress.ip_address(address))
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def is_fetchable(url: str) -> bool:
    """Whether this URL may be fetched at all. Does a DNS lookup, so it blocks."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False

    if parts.scheme not in {"http", "https"}:
        return False

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False

    # `.internal`, `.local` and friends resolve inside the container, not on the
    # internet, and are exactly what an SSRF is aimed at.
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        return False
    if matches_host(host, ALWAYS_BLOCKED_HOSTS):
        return False

    return _host_is_routable(host)


# ------------------------------------------------------------------ extraction


class _Extractor(HTMLParser):
    """Pulls paragraph-shaped text out of one page.

    Text is gathered twice — once from the whole document and once from inside
    an `<article>`/`<main>` — because neither alone is reliable: the whole page
    carries navigation crumbs, and the container guess is wrong often enough on
    sites that render a teaser card inside `<main>`.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._container = 0
        self._buffer: list[str] = []
        self._loose: list[str] = []
        self._scoped: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag in _ARTICLE_TAGS:
            self._container += 1
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag in _ARTICLE_TAGS:
            self._container = max(0, self._container - 1)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if text:
            self._buffer.append(text)

    def _flush(self) -> None:
        if not self._buffer:
            return
        paragraph = " ".join(self._buffer)
        self._buffer.clear()
        if len(paragraph) < MIN_PARAGRAPH_CHARS:
            return
        self._loose.append(paragraph)
        if self._container:
            self._scoped.append(paragraph)

    def close(self) -> None:  # noqa: A003 - HTMLParser's own method name
        super().close()
        self._flush()

    @property
    def loose(self) -> list[str]:
        return self._loose

    @property
    def scoped(self) -> list[str]:
        return self._scoped


def _clean(paragraphs: list[str]) -> list[str]:
    """Drop boilerplate and repeats, keeping the order they appeared in."""
    kept: list[str] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        text = re.sub(r"\s+", " ", paragraph).strip()
        if not text or _BOILERPLATE.match(text):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(text)
    return kept


def _join(paragraphs: list[str], max_chars: int) -> str | None:
    if not paragraphs:
        return None
    text = "\n\n".join(paragraphs)
    return text[:max_chars].rstrip() if len(text) > max_chars else text


def extract_text(html: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> str | None:
    """The article text in `html`, or None when the page has no article in it."""
    parser = _Extractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # a malformed page must never take the request down
        log.info("html parser gave up on a page", exc_info=True)

    scoped = _clean(parser.scoped)
    if sum(len(part) for part in scoped) >= MIN_ARTICLE_CHARS:
        return _join(scoped, max_chars)

    return _join(_clean(parser.loose), max_chars)


def _decode(raw: bytes, declared: str | None) -> str:
    for charset in (declared, "utf-8", "latin-1"):
        if not charset:
            continue
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


def _charset_of(content_type: str | None) -> str | None:
    if not content_type or "charset=" not in content_type.lower():
        return None
    return content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip().strip('"')


async def _hop(
    client: httpx.AsyncClient, url: str, *, timeout: float, max_bytes: int
) -> tuple[int, str | None, str]:
    """One request: `(status, redirect_target, decoded_body)`."""
    async with client.stream(
        "GET",
        url,
        timeout=timeout,
        # Redirects are followed by the caller so that every hop is re-checked
        # against the SSRF rules — httpx following them itself would let a
        # public host bounce the request to a private one.
        follow_redirects=False,
        headers={
            # A page the reader asked to open is fetched the way their browser
            # would fetch it. Announcing a bot token gets the request stalled or
            # served an empty JavaScript shell by a growing number of newsrooms
            # — NPR holds the connection until it times out — which silently
            # turns into "not enough text to summarise".
            "user-agent": USER_AGENT,
            "accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "accept-language": "en",
        },
    ) as response:
        if 300 <= response.status_code < 400:
            return response.status_code, response.headers.get("location"), ""

        if response.status_code >= 400:
            return response.status_code, None, ""

        content_type = response.headers.get("content-type", "")
        if content_type and "html" not in content_type.lower():
            return response.status_code, None, ""

        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) >= max_bytes:
                break

    return response.status_code, None, _decode(bytes(raw), _charset_of(content_type))


async def fetch_body(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_redirects: int = MAX_REDIRECTS,
) -> str | None:
    """The readable text of `url`, or None when there is not enough of it.

    Failures are normal rather than exceptional: paywalls, bot walls and
    JavaScript-only sites all end the same way, with the caller falling back to
    the standfirst. Nothing here raises for a bad page.
    """
    target = url
    for _ in range(max_redirects + 1):
        # DNS runs on a thread: `getaddrinfo` blocks, and this is called from an
        # async request handler.
        if not await asyncio.to_thread(is_fetchable, target):
            log.info("refusing to fetch %s", target)
            return None

        try:
            status, location, html = await _hop(
                client, target, timeout=timeout, max_bytes=max_bytes
            )
        except httpx.HTTPError as exc:
            log.info("could not read %s: %s", target, exc)
            return None

        if location:
            target = urljoin(target, location)
            continue

        if status >= 400 or not html:
            return None

        text = extract_text(html, max_chars=max_chars)
        return text if text and len(text) >= min_chars else None

    log.info("redirect chain too long for %s", url)
    return None
