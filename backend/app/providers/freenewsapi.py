"""FreeNewsApi.io client.

Free tier: 5,000 requests per day at two requests per second. That headroom —
twenty-five times NewsData's — makes it the only provider here that can be
paged through without rationing, so it carries deep scroll and reader mode.

It costs one request per article to use, because `/v1/news` returns only
`uuid`, `title`, `published_at` and `publisher`: no image, no summary and, most
awkwardly, no link. Everything a card needs comes from `/v1/details`, which
also carries the full `body`. Details are immutable, so the aggregator caches
them permanently and pays that request once per article, ever.

Its text search is unusable — the `q` field was withdrawn, `in_title` answers
HTTP 500 and `search=` is silently ignored — so this client deliberately has no
search path. Search stays with NewsAPI and NewsData.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..schemas import ArticleDetail
from .base import (
    FREENEWSAPI,
    ProviderError,
    article_id,
    clean_image,
    clean_text,
    parse_datetime,
    strip_source_suffix,
)

BASE_URL = "https://api.freenewsapi.io/v1"
PROVIDER = FREENEWSAPI

# The free plan answers with exactly ten items per listing whatever
# `page_size` asks for, so paging has to follow `next_cursor` instead.
PAGE_SIZE = 10

# `/v1/topics` — the full vocabulary this provider accepts.
TOPICS = (
    "arts-design",
    "baseball",
    "basketball",
    "beauty",
    "business",
    "celebrities",
    "combat sports",
    "cricket",
    "custom_search",
    "cycling",
    "digital currencies",
    "economy",
    "education",
    "energy",
    "entertainment",
    "environment",
    "fashion",
    "finance",
    "food",
    "football",
    "gadgets",
    "gaming",
    "geology",
    "golf",
    "health",
    "higher education",
    "hockey",
    "home",
    "internet security",
    "jobs",
    "medicine",
    "mental health",
    "mobile",
    "motor sports",
    "movies",
    "music",
    "neuroscience",
    "nutrition",
    "online education",
    "outdoors",
    "paleontology",
    "personal finance",
    "physics",
    "politics",
    "public health",
    "robotics",
    "rugby",
    "science",
    "shopping",
    "soccer",
    "social sciences",
    "space",
    "sports",
    "sports betting",
    "technology",
    "tennis",
    "theater",
    "travel",
    "tv",
    "vehicles",
    "virtual reality",
    "water sports",
    "wildlife",
    "world",
)

# The app's category slugs mapped onto that vocabulary. "top" is absent on
# purpose: it means "the latest across everything", which is a listing with no
# topic filter at all.
CATEGORY_TOPICS = {
    "business": "business",
    "technology": "technology",
    "science": "science",
    "health": "health",
    "sports": "sports",
    "entertainment": "entertainment",
    "politics": "politics",
    "world": "world",
    "environment": "environment",
}

# Put on every article by the crawler, never a real subject.
_IGNORED_TOPICS = {"custom_search"}


class RateLimiter:
    """Spaces calls so the provider never sees more than N per second.

    FreeNewsApi answers a breach with a 429 rather than queueing, so the pause
    is taken before each request while the lock is held. Holding the lock
    across the sleep is what serialises concurrent callers.
    """

    def __init__(self, requests_per_second: float) -> None:
        self._interval = 1.0 / max(requests_per_second, 0.1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


@dataclass(frozen=True, slots=True)
class Listing:
    """A row from `/v1/news`: an id and a headline, nothing renderable yet."""

    uuid: str
    title: str
    source: str | None
    published_at: datetime


async def _get(
    client: httpx.AsyncClient,
    api_key: str,
    limiter: RateLimiter,
    path: str,
    params: dict[str, object],
) -> dict:
    await limiter.acquire()
    response = await client.get(
        f"{BASE_URL}{path}",
        params=params,
        headers={"x-api-key": api_key, "User-Agent": "athenaeum-news/0.2"},
    )

    try:
        body = response.json()
    except ValueError:
        raise ProviderError(
            f"FreeNewsApi returned a non-JSON response (HTTP {response.status_code}).",
            provider=PROVIDER,
            status_code=response.status_code,
        ) from None

    message = clean_text(body.get("error")) if isinstance(body, dict) else None

    if response.status_code == 401:
        raise ProviderError(
            message or "FreeNewsApi rejected the API key.",
            provider=PROVIDER,
            status_code=401,
        )

    if response.status_code == 429:
        raise ProviderError(
            message or "FreeNewsApi rate limit reached.",
            provider=PROVIDER,
            status_code=429,
            quota_exhausted=True,
        )

    if response.status_code >= 400:
        raise ProviderError(
            message or f"FreeNewsApi returned HTTP {response.status_code}.",
            provider=PROVIDER,
            status_code=response.status_code,
        )

    if not isinstance(body, dict):
        raise ProviderError(
            "FreeNewsApi returned an unexpected payload.",
            provider=PROVIDER,
            status_code=response.status_code,
        )

    return body


async def fetch_listing(
    client: httpx.AsyncClient,
    api_key: str,
    limiter: RateLimiter,
    *,
    category: str | None = None,
    cursor: str | None = None,
    language: str = "en",
) -> tuple[list[Listing], str | None]:
    """Return `(rows, next_cursor)` for one page of ten."""
    params: dict[str, object] = {"language": language}
    topic = CATEGORY_TOPICS.get(category or "")
    if topic:
        params["topic"] = topic
    if cursor:
        params["cursor"] = cursor

    body = await _get(client, api_key, limiter, "/news", params)

    rows: list[Listing] = []
    for raw in body.get("data") or []:
        if not isinstance(raw, dict):
            continue
        uuid = clean_text(raw.get("uuid"))
        title = clean_text(raw.get("title"))
        if not uuid or not title:
            continue
        source = clean_text(raw.get("publisher"))
        rows.append(
            Listing(
                uuid=uuid,
                title=strip_source_suffix(title, source),
                source=source,
                published_at=parse_datetime(raw.get("published_at")),
            )
        )

    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    return rows, clean_text(meta.get("next_cursor"))


async def fetch_details(
    client: httpx.AsyncClient,
    api_key: str,
    limiter: RateLimiter,
    uuid: str,
) -> ArticleDetail | None:
    """Hydrate one article, or return None if it is no longer retrievable.

    A uuid that has aged out answers 400 "Invalid uuid"; that is a normal
    outcome for a feed cursor that has gone stale, not a failure worth raising.
    """
    try:
        body = await _get(client, api_key, limiter, "/details", {"uuid": uuid})
    except ProviderError as exc:
        if exc.status_code in {400, 404}:
            return None
        raise

    raw = body.get("data")
    return _to_detail(raw) if isinstance(raw, dict) else None


def _to_detail(raw: dict) -> ArticleDetail | None:
    url = clean_text(raw.get("original_url"))
    source = clean_text(raw.get("publisher"))
    title = clean_text(raw.get("title"))
    if not url or not title:
        return None
    title = strip_source_suffix(title, source)

    authors = _as_list(raw.get("authors"))
    topics = [topic for topic in _as_list(raw.get("topics")) if topic not in _IGNORED_TOPICS]
    body = clean_text(raw.get("body"))

    return ArticleDetail(
        id=article_id(url, title),
        title=title,
        description=clean_text(raw.get("incipit")),
        url=url,
        image_url=clean_image(raw.get("thumbnail")),
        source=source,
        source_icon=None,
        author=", ".join(authors) or None,
        published_at=parse_datetime(raw.get("published_at")),
        categories=topics,
        countries=_as_list(raw.get("countries")),
        sentiment=None,
        provider=PROVIDER,
        body=body,
        incipit=clean_text(raw.get("incipit")),
        authors=authors,
        topics=topics,
    )


def _as_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [item for item in (clean_text(entry) for entry in value) if item]
    single = clean_text(value)
    return [single] if single else []
