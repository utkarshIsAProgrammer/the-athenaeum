"""Reading a story at its publisher, safely.

The extraction tests are offline by construction — they are handed HTML. The
fetch tests are offline too: `is_fetchable` is replaced so no DNS lookup is
attempted, and the requests go to an `httpx.MockTransport`. Nothing here opens
a socket.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from app.services import reader

PARAGRAPH = "The council approved the measure on Thursday after a six-hour session."
SECOND = "Work is expected to begin in March and to run through the summer."
THIRD = "Opponents have said they will appeal the decision to the high court."


def page(*, body: str, chrome: str = "") -> str:
    return (
        "<html><head><title>A story</title><style>body{color:red}</style></head>"
        f"<body>{chrome}{body}<p>Subscribe to our newsletter</p></body></html>"
    )


ARTICLE = f"<article><h1>Council approves the measure</h1><p>{PARAGRAPH}</p><p>{SECOND}</p><p>{THIRD}</p></article>"


# ------------------------------------------------------------------ extraction


def test_extract_text_keeps_the_prose_and_drops_the_chrome() -> None:
    text = reader.extract_text(
        page(chrome="<nav>Home World Sport</nav>", body=ARTICLE)
    )

    assert text is not None
    assert PARAGRAPH in text
    assert SECOND in text
    # Navigation and the newsletter prompt are not the story.
    assert "Home World Sport" not in text
    assert "Subscribe" not in text
    assert "color:red" not in text


def test_extract_text_prefers_the_article_container_once_it_has_enough() -> None:
    # A long block outside `<article>` is the page around the story — a sidebar
    # of related links, most often. It must lose to the container.
    aside = "<div>" + ("An unrelated promotional block about our own products. " * 10) + "</div>"
    body = "<article>" + "".join(f"<p>{PARAGRAPH} {n}</p>" for n in range(8)) + "</article>"

    text = reader.extract_text(page(chrome=aside, body=body))

    assert text is not None
    assert PARAGRAPH in text
    assert "unrelated promotional block" not in text


def test_extract_text_falls_back_to_the_whole_page_when_there_is_no_container() -> None:
    body = f"<div><p>{PARAGRAPH}</p><p>{SECOND}</p></div>"

    text = reader.extract_text(page(body=body))

    assert text is not None
    assert PARAGRAPH in text


def test_extract_text_refuses_a_page_with_nothing_to_read() -> None:
    assert reader.extract_text(page(body="<p>Hi.</p>")) is None


def test_extract_text_survives_a_malformed_page() -> None:
    # Never raising matters more than recovering every paragraph: this runs
    # inside a request handler.
    text = reader.extract_text(
        "<html><body><article><p>" + PARAGRAPH + "<p>" + SECOND
    )

    assert text is None or PARAGRAPH in text


def test_extract_text_stops_at_the_character_ceiling() -> None:
    body = "<article>" + "".join(f"<p>{PARAGRAPH} {n}</p>" for n in range(20)) + "</article>"

    text = reader.extract_text(page(body=body), max_chars=120)

    assert text is not None
    assert len(text) <= 120


def test_a_repeated_paragraph_is_kept_once() -> None:
    body = "<article>" + f"<p>{PARAGRAPH}</p><p>{PARAGRAPH}</p>" + f"<p>{SECOND}</p>" + "</article>"

    text = reader.extract_text(page(body=body))

    assert text is not None
    assert text.count(PARAGRAPH) == 1


# ---------------------------------------------------------------- SSRF guards


def addresses(*ips: str) -> Any:
    """A `getaddrinfo` stand-in returning the addresses given."""

    def fake(host: str, port: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]

    return fake


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "http://localhost/x",
        "http://api.internal/x",
        "http://printer.local/x",
        "http://box.home.arpa/x",
        "http:///nohost",
    ],
)
def test_a_url_that_cannot_be_reached_is_refused(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reader.socket, "getaddrinfo", addresses("93.184.216.34"))

    assert reader.is_fetchable(url) is False


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.4.4", "169.254.169.254", "0.0.0.0", "::1"],
)
def test_a_host_that_resolves_inside_the_network_is_refused(
    ip: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 169.254.169.254 is the cloud metadata service, which is the point of the
    # whole check.
    monkeypatch.setattr(reader.socket, "getaddrinfo", addresses(ip))

    assert reader.is_fetchable("https://news.example.com/story") is False


def test_one_private_address_among_public_ones_is_enough_to_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reader.socket, "getaddrinfo", addresses("93.184.216.34", "127.0.0.1")
    )

    assert reader.is_fetchable("https://news.example.com/story") is False


def test_a_host_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*_args: Any, **_kwargs: Any) -> Any:
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(reader.socket, "getaddrinfo", failing)

    assert reader.is_fetchable("https://nowhere.example/story") is False


def test_an_ordinary_publisher_is_fetchable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader.socket, "getaddrinfo", addresses("93.184.216.34"))

    assert reader.is_fetchable("https://www.example.com/story") is True
    # A trailing dot is the same host, and a redirect may add one.
    assert reader.is_fetchable("https://example.com./story") is True


def test_a_nat64_address_beside_a_real_one_does_not_condemn_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `www.bbc.com` resolves to `64:ff9b::9765:6851` — a public Fastly address
    # wearing the NAT64 prefix — alongside its ordinary A records. Judging the
    # wrapper refused a perfectly good newsroom, which is a silent source of
    # "not enough text to summarise".
    monkeypatch.setattr(
        reader.socket, "getaddrinfo", addresses("64:ff9b::9765:6851", "151.101.104.81")
    )

    assert reader.is_fetchable("https://www.bbc.com/news/story") is True


def test_a_nat64_address_hiding_a_private_host_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 64:ff9b::a00:1 is 10.0.0.1, and unwrapping must not become a way through.
    monkeypatch.setattr(reader.socket, "getaddrinfo", addresses("64:ff9b::a00:1"))

    assert reader.is_fetchable("https://internal.example.com/story") is False


# -------------------------------------------------------------------- fetching


@pytest.fixture
def allow_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "is_fetchable", lambda url: True)


def client_for(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_page_is_read_and_its_text_returned(allow_all: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=page(body=ARTICLE),
        )

    async with client_for(handler) as client:
        text = await reader.fetch_body(client, "https://example.com/story", timeout=5)

    assert text is not None
    assert PARAGRAPH in text


async def test_the_request_looks_like_a_browser_and_not_a_bot(allow_all: None) -> None:
    # Several newsrooms stall or answer with an empty shell when the request
    # announces a bot, and the reader has no way to tell that apart from a
    # paywall — it just quietly reports there was nothing to summarise.
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("user-agent", ""))
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=page(body=ARTICLE)
        )

    async with client_for(handler) as client:
        await reader.fetch_body(client, "https://example.com/story", timeout=5)

    assert sent == [reader.USER_AGENT]
    assert sent[0].startswith("Mozilla/5.0 (")
    assert "bot" not in sent[0].lower()


async def test_a_page_with_too_little_text_is_treated_as_a_failure(allow_all: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=page(body="<p>Short.</p>")
        )

    async with client_for(handler) as client:
        text = await reader.fetch_body(client, "https://example.com/story", timeout=5)

    assert text is None


async def test_a_redirect_is_followed_by_hand_and_the_target_is_checked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url).endswith("/original"):
            return httpx.Response(302, headers={"location": "/moved"})
        # A public host bouncing the reader at something private is the attack
        # this exists to stop, so the second hop must not be requested at all.
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=page(body=ARTICLE)
        )

    monkeypatch.setattr(
        reader, "is_fetchable", lambda url: url.endswith("/original")
    )

    async with client_for(handler) as client:
        text = await reader.fetch_body(client, "https://example.com/original", timeout=5)

    assert text is None
    assert seen == ["https://example.com/original"]


async def test_a_redirect_chain_that_ends_somewhere_readable_is_followed(
    allow_all: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/original"):
            return httpx.Response(301, headers={"location": "https://example.com/final"})
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=page(body=ARTICLE)
        )

    async with client_for(handler) as client:
        text = await reader.fetch_body(client, "https://example.com/original", timeout=5)

    assert text is not None
    assert PARAGRAPH in text


async def test_a_redirect_loop_ends_rather_than_spinning(allow_all: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    async with client_for(handler) as client:
        text = await reader.fetch_body(client, "https://example.com/loop", timeout=5)

    assert text is None


async def test_a_paywall_is_not_read_as_an_article(allow_all: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402)

    async with client_for(handler) as client:
        text = await reader.fetch_body(client, "https://example.com/story", timeout=5)

    assert text is None


async def test_something_that_is_not_html_is_left_alone(allow_all: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    async with client_for(handler) as client:
        text = await reader.fetch_body(client, "https://example.com/paper.pdf", timeout=5)

    assert text is None


async def test_a_huge_page_is_read_only_up_to_the_byte_cap(allow_all: None) -> None:
    big = "<html><body><article>" + "".join(
        f"<p>{PARAGRAPH} {n}</p>" for n in range(4000)
    ) + "</article></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=big)

    async with client_for(handler) as client:
        text = await reader.fetch_body(
            client, "https://example.com/huge", timeout=5, max_bytes=20_000
        )

    assert text is not None
    assert len(text) <= reader.DEFAULT_MAX_CHARS


async def test_a_transport_failure_is_not_an_exception(allow_all: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with client_for(handler) as client:
        text = await reader.fetch_body(client, "https://example.com/story", timeout=5)

    assert text is None
