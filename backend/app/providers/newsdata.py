"""NewsData.io client.

Free tier: 200 credits per day, 10 articles per credit (~12h publication
delay). It is the primary provider for browsing because it is the only one of
the two that returns real images, sentiment and country metadata — NewsAPI's
free tier is capped at 100 requests/day and returns no imagery guarantee.

`content` and `ai_summary` come back as the literal string
"ONLY AVAILABLE IN PAID PLANS" on the free plan and are dropped in
`base.clean_text`.
"""

from __future__ import annotations

import httpx

from ..schemas import Article
from .base import NEWSDATA, ProviderError, article_id, clean_image, clean_text, parse_datetime

BASE_URL = "https://newsdata.io/api/1/latest"
PROVIDER = NEWSDATA

CATEGORIES = (
    "business",
    "crime",
    "domestic",
    "education",
    "entertainment",
    "environment",
    "food",
    "health",
    "lifestyle",
    "other",
    "politics",
    "science",
    "sports",
    "technology",
    "top",
    "tourism",
    "world",
)

# The free plan serves at most 10 articles per request.
MAX_PAGE_SIZE = 10


async def fetch(
    client: httpx.AsyncClient,
    api_key: str,
    *,
    category: str | None = None,
    country: str | None = None,
    query: str | None = None,
    page: str | None = None,
    page_size: int = MAX_PAGE_SIZE,
    language: str = "en",
) -> tuple[list[Article], str | None]:
    """Return `(articles, next_cursor)`; the cursor is NewsData's `nextPage` token."""
    params: dict[str, object] = {
        "apikey": api_key,
        "language": language,
        "size": min(page_size, MAX_PAGE_SIZE),
        "image": 1,
    }
    if category and category != "general":
        params["category"] = category
    if country:
        params["country"] = country
    if query:
        params["q"] = query
    if page:
        params["page"] = page

    response = await client.get(BASE_URL, params=params)

    try:
        body = response.json()
    except ValueError:
        raise ProviderError(
            f"NewsData returned a non-JSON response (HTTP {response.status_code}).",
            provider=PROVIDER,
            status_code=response.status_code,
        ) from None

    if not isinstance(body, dict):
        raise ProviderError(
            "NewsData returned an unexpected payload.",
            provider=PROVIDER,
            status_code=response.status_code,
        )

    if body.get("status") == "error":
        detail = body.get("results")
        message = None
        code = ""
        if isinstance(detail, dict):
            message = clean_text(detail.get("message"))
            code = (clean_text(detail.get("code")) or "").lower()
        elif isinstance(detail, str):
            message = clean_text(detail)
        raise ProviderError(
            message or "NewsData rejected the request.",
            provider=PROVIDER,
            status_code=response.status_code,
            quota_exhausted="quota" in code or "limit" in code or response.status_code == 429,
        )

    if response.status_code == 429:
        raise ProviderError(
            "NewsData daily quota exhausted.",
            provider=PROVIDER,
            status_code=429,
            quota_exhausted=True,
        )

    if response.status_code >= 400:
        raise ProviderError(
            f"NewsData returned HTTP {response.status_code}.",
            provider=PROVIDER,
            status_code=response.status_code,
        )

    articles = [_to_article(raw) for raw in body.get("results") or []]
    cursor = clean_text(body.get("nextPage"))
    return [article for article in articles if article is not None], cursor


def _to_article(raw: dict) -> Article | None:
    title = clean_text(raw.get("title"))
    url = clean_text(raw.get("link"))
    if not title or not url:
        return None

    categories = _as_list(raw.get("category"))
    countries = _as_list(raw.get("country"))

    return Article(
        id=article_id(url, title),
        title=title,
        description=clean_text(raw.get("description")),
        url=url,
        image_url=clean_image(raw.get("image_url")),
        source=clean_text(raw.get("source_name")) or clean_text(raw.get("source_id")),
        source_icon=clean_text(raw.get("source_icon")),
        author=clean_text(raw.get("creator")),
        published_at=parse_datetime(raw.get("pubDate")),
        categories=categories,
        countries=countries,
        sentiment=clean_text(raw.get("sentiment")),
        provider=PROVIDER,
    )


def _as_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [item for item in (clean_text(entry) for entry in value) if item]
    single = clean_text(value)
    return [single] if single else []
