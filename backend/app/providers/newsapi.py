"""NewsAPI.org client.

Free tier limits that shape how the aggregator uses this provider:
  * 100 requests per day in total;
  * `/v2/top-headlines` only accepts a 24-hour-old index, so it is not the
    freshest source;
  * `/v2/everything` searches a rolling one-month window.

It is therefore used as the *search* provider, where its query syntax is much
stronger than NewsData's, and only as a fallback for category browsing.
"""

from __future__ import annotations

import httpx

from ..schemas import Article
from .base import NEWSAPI, ProviderError, article_id, clean_image, clean_text, parse_datetime

BASE_URL = "https://newsapi.org/v2"
PROVIDER = NEWSAPI

HEADLINE_CATEGORIES = (
    "business",
    "entertainment",
    "general",
    "health",
    "science",
    "sports",
    "technology",
)

# Free-tier responses are capped well below this, but asking for more is free.
MAX_PAGE_SIZE = 100


async def _get(client: httpx.AsyncClient, api_key: str, path: str, params: dict) -> dict:
    response = await client.get(
        f"{BASE_URL}{path}",
        params=params,
        headers={"X-Api-Key": api_key, "User-Agent": "athenaeum-news/0.2"},
    )

    try:
        body = response.json()
    except ValueError:
        raise ProviderError(
            f"NewsAPI returned a non-JSON response (HTTP {response.status_code}).",
            provider=PROVIDER,
            status_code=response.status_code,
        ) from None

    # NewsAPI signals most failures inside a 200 body rather than via status.
    if isinstance(body, dict) and body.get("status") == "error":
        code = clean_text(body.get("code")) or ""
        raise ProviderError(
            clean_text(body.get("message")) or "NewsAPI rejected the request.",
            provider=PROVIDER,
            status_code=response.status_code,
            quota_exhausted=code in {"rateLimited", "maximumResultsReached"},
        )

    if response.status_code == 429:
        raise ProviderError(
            "NewsAPI daily quota exhausted.",
            provider=PROVIDER,
            status_code=429,
            quota_exhausted=True,
        )

    if response.status_code >= 400:
        raise ProviderError(
            f"NewsAPI returned HTTP {response.status_code}.",
            provider=PROVIDER,
            status_code=response.status_code,
        )

    return body


async def fetch(
    client: httpx.AsyncClient,
    api_key: str,
    *,
    category: str | None = None,
    country: str | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = MAX_PAGE_SIZE,
) -> list[Article]:
    """Fetch either search results (`query`) or category headlines."""
    if query:
        body = await _get(
            client,
            api_key,
            "/everything",
            {
                "q": query,
                "searchIn": "title,description",
                "sortBy": "publishedAt",
                "language": "en",
                "page": page,
                "pageSize": min(page_size, MAX_PAGE_SIZE),
            },
        )
    else:
        params = {"country": country or "us", "page": page, "pageSize": min(page_size, MAX_PAGE_SIZE)}
        if category and category != "general":
            params["category"] = category
        body = await _get(client, api_key, "/top-headlines", params)

    articles = [_to_article(raw, category=category) for raw in body.get("articles") or []]
    return [article for article in articles if article is not None]


def _to_article(raw: dict, *, category: str | None) -> Article | None:
    title = clean_text(raw.get("title"))
    url = clean_text(raw.get("url"))
    if not title or not url:
        return None

    source = raw.get("source") or {}
    categories = [category] if category and category != "general" else []

    return Article(
        id=article_id(url, title),
        title=title,
        description=clean_text(raw.get("description")),
        url=url,
        image_url=clean_image(raw.get("urlToImage")),
        source=clean_text(source.get("name")) if isinstance(source, dict) else clean_text(source),
        source_icon=None,
        author=clean_text(raw.get("author")),
        published_at=parse_datetime(raw.get("publishedAt")),
        categories=categories,
        countries=[],
        sentiment=None,
        provider=PROVIDER,
    )
