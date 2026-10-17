"""Merges three free news providers behind one cached, de-duplicated feed.

Why cache-first: the three free tiers allow 100 (NewsAPI) + 200 (NewsData) +
5,000 (FreeNewsApi) requests per day. Anything that fires a request per page
view would exhaust the first two in minutes, so every distinct query is
resolved once and then served from SQLite until its TTL expires.

Provider roles, which differ because their free tiers differ:

  * **NewsAPI** — search. Its query syntax is the only usable one, but 100
    requests/day means it must never carry ordinary browsing.
  * **NewsData** — the front page. Ten rich items per request with images,
    summaries and sentiment, at 200 requests/day.
  * **FreeNewsApi** — everything after that. A listing is ten bare items and
    each one costs a second call for its image, summary and link, but 5,000 a
    day is enough headroom to page through without rationing. It also supplies
    article bodies for reader mode.

Each page is collapsed with `clustering.cluster`, so a story five outlets
covered appears once with the rest named beneath it.

Degradation ladder, in order:
  1. fresh cache hit      -> serve it, no upstream traffic at all;
  2. stale cache hit      -> serve it and renew it behind the reader;
  3. upstream success     -> merge, cache, serve;
  4. all upstreams failed -> serve the last good response even if expired;
  5. nothing cached       -> empty feed with a populated `meta.quotas`.

Only the feed expires. A story's own text is recovered once, when the story is
opened, and kept forever — see `article`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Coroutine
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ..config import Settings
from ..db import Database
from ..providers import freenewsapi, newsapi, newsdata
from ..providers.base import FREENEWSAPI, NEWSAPI, NEWSDATA, ProviderError
from ..providers.freenewsapi import Listing, RateLimiter
from ..schemas import (
    Article,
    ArticleDetail,
    FeedMeta,
    FeedResponse,
    QuotaState,
    StoryCluster,
    StoryThread,
    TrendingResponse,
    TrendingStory,
    TrendingTopic,
)
from . import quality, reader
from .clustering import cluster, richness

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MIN_LIMIT = 5

# How many in-flight page resolutions the lock table remembers. Past this,
# idle locks are dropped: the worst case that leaves is two callers racing for
# one cold page, which is where they started anyway.
MAX_LOCKS = 128

# Every upstream call asks for the maximum and the result is cached whole, so
# a later request for a smaller page is free and the top-stories cache can be
# shared with `/api/trending`.
FETCH_LIMIT = MAX_LIMIT

# A category the frontend can ask for, mapped to the provider's own vocabulary.
# Anything absent from a provider's map falls back to that provider's default.
NEWSAPI_CATEGORY = {
    "business": "business",
    "entertainment": "entertainment",
    "health": "health",
    "science": "science",
    "sports": "sports",
    "technology": "technology",
}

# The canonical category list the client renders as chips. NewsData's taxonomy
# is the superset, so it is used directly.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("top", "Top Stories"),
    ("business", "Business"),
    ("technology", "Technology"),
    ("science", "Science"),
    ("health", "Health"),
    ("sports", "Sports"),
    ("entertainment", "Entertainment"),
    ("politics", "Politics"),
    ("world", "World"),
    ("environment", "Environment"),
)

CATEGORY_LABELS = dict(CATEGORIES)

# Our page tokens are FreeNewsApi's own cursor behind a prefix, so the client
# treats them as opaque and the provider can be swapped later. The empty cursor
# means "the top of the listing".
CURSOR_PREFIX = "fns:"


class _Page(BaseModel):
    """One cached feed page: its articles, its clusters and how to get more."""

    articles: list[Article] = Field(default_factory=list)
    clusters: list[StoryCluster] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


def _cache_key(scope: str, **params: object) -> str:
    serialized = json.dumps(params, sort_keys=True, default=str)
    return f"{scope}:{hashlib.sha1(serialized.encode('utf-8')).hexdigest()[:16]}"


def _encode(page: _Page) -> bytes:
    return page.model_dump_json().encode("utf-8")


def _decode(payload: bytes) -> _Page:
    return _Page.model_validate_json(payload)


def _merge(buckets: list[list[Article]]) -> list[Article]:
    """De-duplicate across providers, keeping the richer copy, newest first.

    The same story syndicated to one URL by two providers is resolved on
    data quality rather than arrival order, so a search that fell back to
    NewsAPI cannot cost the card its image.

    Only literal repeats are dropped here — different outlets' wording of the
    same story is `clustering.cluster`'s job.
    """
    candidates = sorted(
        (article for bucket in buckets for article in bucket),
        key=richness,
        reverse=True,
    )

    best: dict[str, Article] = {}
    for article in candidates:
        best.setdefault(article.id, article)

    return sorted(best.values(), key=lambda article: article.published_at, reverse=True)


def _threads_from(
    clusters: list[StoryCluster], coverage: dict[str, list[Article]]
) -> list[StoryThread]:
    """Pair each cluster's summary with the copies it folded together.

    The cluster travels in the feed payload and dies with it; the thread is
    written to the database so a reader who reopens the story an hour later is
    still told who else ran it.
    """
    return [
        StoryThread(
            lead_id=story.lead_id,
            count=story.count,
            sources=story.sources,
            coverage=coverage.get(story.lead_id, []),
        )
        for story in clusters
    ]


class NewsAggregator:
    def __init__(self, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._db = database
        self._client: httpx.AsyncClient | None = None
        self._limiter = RateLimiter(settings.freenewsapi_requests_per_second)
        self._blocked_hosts = settings.feed_blocked_host_list

        # One page is resolved once however many readers ask for it at the same
        # moment: the first holds the lock and fetches, the rest take the
        # cached answer. Bodies are locked per article for the same reason.
        self._locks: dict[str, asyncio.Lock] = {}
        # Keys with a renewal already running, so a stale page flushes once.
        self._refreshing: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        # Bounds how many publisher pages are being read at any one moment.
        self._reader_gate = asyncio.Semaphore(max(1, settings.reader_concurrency))

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=self._settings.request_timeout_seconds,
            follow_redirects=True,
            # One pooled client is shared by every caller, so these ceilings are
            # per-process rather than per-request. Kept deliberately small: this
            # runs on a free instance, and the providers rate-limit regardless.
            limits=httpx.Limits(
                max_connections=max(8, self._settings.reader_concurrency * 4),
                max_keepalive_connections=max(4, self._settings.reader_concurrency * 2),
                keepalive_expiry=30.0,
            ),
        )

    async def shutdown(self) -> None:
        # A renewal that is still running would outlive the client it borrows.
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._refreshing.clear()

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------ public API

    async def feed(
        self,
        *,
        category: str | None = None,
        country: str | None = None,
        query: str | None = None,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> FeedResponse:
        limit = max(MIN_LIMIT, min(limit, MAX_LIMIT))
        key = _cache_key("feed", category=category, country=country, query=query, cursor=cursor)

        cached = self._cached_page(key)
        if cached is not None:
            page, fresh = cached
            if fresh:
                log.info("cache hit %s (%d articles)", key, len(page.articles))
                return self._respond(page, limit=limit, cached=True, stale=False, providers=[])
            # Stale but still inside the grace window: answer from it now and
            # renew it behind the reader. Nobody waits on a provider that is
            # merely slow, and however many readers arrive at once, the page is
            # fetched from upstream exactly once.
            log.info("cache stale %s: serving it and renewing behind", key)
            self._renew_later(
                key, category=category, country=country, query=query, cursor=cursor
            )
            return self._respond(page, limit=limit, cached=True, stale=True, providers=[])

        page, providers, served_stale = await self._resolve(
            key, category=category, country=country, query=query, cursor=cursor
        )
        return self._respond(
            page, limit=limit, cached=served_stale, stale=False, providers=providers
        )

    async def article(self, article_id: str, *, recover: bool = True) -> ArticleDetail | None:
        """Reader mode: whatever we know about one article, body included.

        Articles reach this table two ways — as a summary whenever a feed
        served them, and with a full body once FreeNewsApi's detail call was
        made. The other two providers never supply a body, so when one is
        missing the publisher's own page is read once and its text kept
        forever. That costs no provider quota and is what a summary is written
        from.

        Pass `recover=False` for a caller that only wants what is stored.
        """
        stored = self._db.article_get(article_id)
        if stored is None:
            return None
        if not recover or stored.body or not self._settings.reader_enabled:
            return stored
        return await self._recover_body(stored) or stored

    async def _recover_body(self, article: ArticleDetail) -> ArticleDetail | None:
        """Read the story at its publisher and remember the outcome.

        The outcome matters as much as the text: a page that will never yield
        one is recorded as *tried*, so a paywall is not re-fetched on every
        view. Returns None whenever there is nothing new to return.
        """
        checked = self._db.article_body_checked_at(article.id)
        if checked is not None:
            age = (datetime.now(timezone.utc) - checked).total_seconds()
            if age < self._settings.reader_retry_seconds:
                return None
        if quality.is_blocked(article, blocked_hosts=self._blocked_hosts):
            return None

        async with self._lock_for(f"body:{article.id}"):
            # Another request may have completed while this one waited.
            current = self._db.article_get(article.id)
            if current is not None and current.body:
                return current

            try:
                body = await self._read_publisher(article.url)
            except ProviderError:  # pragma: no cover - the client is always up
                return None

            return self._db.article_record_body(article.id, body)

    async def _read_publisher(self, url: str) -> str | None:
        """One publisher page, through the shared client and the reader gate."""
        async with self._reader_gate:
            return await reader.fetch_body(
                self.require_client(),
                url,
                timeout=self._settings.reader_timeout_seconds,
                max_bytes=self._settings.reader_max_bytes,
                max_chars=self._settings.reader_max_chars,
                min_chars=self._settings.reader_min_chars,
            )

    async def trending(self, *, limit: int = 12) -> TrendingResponse:
        """Stories picked out because more than one outlet ran them.

        Reads the same cached top-stories page the feed uses, so it costs no
        extra quota once the front page has been loaded.
        """
        page = await self.feed(category="top", limit=MAX_LIMIT)

        stories: list[TrendingStory] = []
        ranked = sorted(page.meta.clusters, key=lambda item: item.count, reverse=True)
        for story in ranked[:limit]:
            lead = self._db.article_get(story.lead_id)
            if lead is None:
                continue
            stories.append(
                TrendingStory(
                    id=lead.id,
                    title=lead.title,
                    url=lead.url,
                    source=lead.source,
                    image_url=lead.image_url,
                    published_at=lead.published_at,
                    sources=story.sources,
                )
            )

        counts = Counter(
            slug
            for article in page.articles
            for slug in article.categories
            # "top" is a placement, not a subject, and NewsData tags most of
            # its output with it — counting it would swamp the real topics.
            if slug in CATEGORY_LABELS and slug != "top"
        )
        topics = [
            TrendingTopic(slug=slug, label=CATEGORY_LABELS[slug], count=count)
            for slug, count in counts.most_common(8)
        ]

        return TrendingResponse(stories=stories, topics=topics)

    def quotas(self) -> list[QuotaState]:
        return [
            self._quota(NEWSAPI, self._settings.newsapi_daily_limit),
            self._quota(NEWSDATA, self._settings.newsdata_daily_limit),
            self._quota(FREENEWSAPI, self._settings.freenewsapi_daily_limit),
        ]

    # --------------------------------------------------------------- fetches

    async def _collect(
        self,
        *,
        category: str | None,
        country: str | None,
        query: str | None,
        cursor: str | None,
    ) -> tuple[_Page, list[str]]:
        """Resolve one page, tolerating partial provider failure."""
        if cursor is not None:
            return await self._collect_more(category=category, cursor=self._strip_cursor(cursor))

        # A free-text query favours NewsAPI (its search syntax is far better);
        # a category browse favours NewsData (fresher, and the only free source
        # of images and summaries). The other provider is capped so it cannot
        # dominate the page.
        preferred, secondary = (NEWSAPI, NEWSDATA) if query else (NEWSDATA, NEWSAPI)
        caps = {preferred: FETCH_LIMIT, secondary: max(FETCH_LIMIT // 3, MIN_LIMIT)}

        jobs: dict[str, Coroutine[Any, Any, list[Article]]] = {}
        for provider in (preferred, secondary):
            if not self._ready(provider):
                continue
            jobs[provider] = self._dispatch(
                provider, category=category, country=country, query=query, limit=FETCH_LIMIT
            )

        if not jobs:
            raise ProviderError(
                "Daily quota reached for every configured provider.",
                provider="all",
                quota_exhausted=True,
            )

        results = await asyncio.gather(*jobs.values(), return_exceptions=True)

        buckets: list[list[Article]] = []
        providers: list[str] = []
        for provider, result in zip(jobs.keys(), results):
            if isinstance(result, BaseException):
                log.warning("%s request failed: %s", provider, result)
                continue
            providers.append(provider)
            buckets.append(result[: caps[provider]])

        if not providers:
            raise ProviderError("Every provider request failed.", provider="all")

        merged = _merge(buckets)
        merged = quality.keep(merged, blocked_hosts=self._blocked_hosts)
        self._db.articles_remember(merged, uuid_of={})
        articles, clusters, coverage = cluster(merged)
        self._db.threads_remember(_threads_from(clusters, coverage))

        # FreeNewsApi cannot search, so a search result list has nothing to
        # page through and simply ends.
        can_page = query is None and self._ready(FREENEWSAPI)
        return (
            _Page(
                articles=articles,
                clusters=clusters,
                next_cursor=CURSOR_PREFIX if can_page else None,
                has_more=can_page,
            ),
            providers,
        )

    async def _collect_more(self, *, category: str | None, cursor: str) -> tuple[_Page, list[str]]:
        """Page deeper through FreeNewsApi, hydrating each article as it goes."""
        if not self._ready(FREENEWSAPI):
            raise ProviderError(
                "FreeNewsApi is not configured or its daily quota is spent.",
                provider=FREENEWSAPI,
                quota_exhausted=not self._available(FREENEWSAPI),
            )

        rows, next_token = await self._fetch_listing(category=category, cursor=cursor or None)
        articles, uuid_of = await self._hydrate(rows)
        articles = quality.keep(articles, blocked_hosts=self._blocked_hosts)
        self._db.articles_remember(articles, uuid_of=uuid_of)

        articles, clusters, coverage = cluster(articles)
        self._db.threads_remember(_threads_from(clusters, coverage))
        return (
            _Page(
                articles=articles,
                clusters=clusters,
                next_cursor=f"{CURSOR_PREFIX}{next_token}" if next_token else None,
                has_more=bool(next_token),
            ),
            [FREENEWSAPI],
        )

    def _dispatch(self, provider: str, **kwargs: Any) -> Coroutine[Any, Any, list[Article]]:
        if provider == NEWSAPI:
            return self._fetch_newsapi(**kwargs)
        return self._fetch_newsdata(**kwargs)

    async def _fetch_newsapi(
        self, *, category: str | None, country: str | None, query: str | None, limit: int
    ) -> list[Article]:
        # `/v2/top-headlines` mandates a country or a source list.
        mapped = NEWSAPI_CATEGORY.get(category or "", "general")
        try:
            return await newsapi.fetch(
                self.require_client(),
                self._settings.newsapi_key,
                category=mapped,
                country=country or "us",
                query=query,
                page_size=limit,
            )
        finally:
            self._db.usage_record(NEWSAPI)

    async def _fetch_newsdata(
        self, *, category: str | None, country: str | None, query: str | None, limit: int
    ) -> list[Article]:
        try:
            articles, _cursor = await newsdata.fetch(
                self.require_client(),
                self._settings.newsdata_key,
                category=category,
                country=country,
                query=query,
                page_size=limit,
            )
            return articles
        finally:
            self._db.usage_record(NEWSDATA)

    async def _fetch_listing(
        self, *, category: str | None, cursor: str | None
    ) -> tuple[list[Listing], str | None]:
        try:
            return await freenewsapi.fetch_listing(
                self.require_client(),
                self._settings.freenewsapi_key,
                self._limiter,
                category=category,
                cursor=cursor,
            )
        finally:
            self._db.usage_record(FREENEWSAPI)

    async def _fetch_detail(self, uuid: str) -> ArticleDetail | None:
        try:
            return await freenewsapi.fetch_details(
                self.require_client(),
                self._settings.freenewsapi_key,
                self._limiter,
                uuid,
            )
        finally:
            self._db.usage_record(FREENEWSAPI)

    # ---------------------------------------------------------------- hydrate

    async def _hydrate(self, rows: list[Listing]) -> tuple[list[ArticleDetail], dict[str, str]]:
        """Turn bare listing rows into renderable articles.

        A row carries no link, so an article that cannot be hydrated is dropped
        rather than shown as a card that leads nowhere. Bodies already in the
        database skip the request entirely.
        """
        budget = max(0, self._settings.freenewsapi_detail_budget)
        rows = rows[:budget]
        if not rows:
            return [], {}

        known = self._db.articles_by_uuid([row.uuid for row in rows])

        articles: list[ArticleDetail] = []
        uuid_of: dict[str, str] = {}
        pending: list[Listing] = []
        for row in rows:
            cached = known.get(row.uuid)
            if cached is not None:
                articles.append(cached)
                uuid_of[cached.id] = row.uuid
            else:
                pending.append(row)

        if pending:
            results = await asyncio.gather(
                *(self._fetch_detail(row.uuid) for row in pending),
                return_exceptions=True,
            )
            for row, result in zip(pending, results):
                if isinstance(result, BaseException):
                    log.warning("freenewsapi detail %s failed: %s", row.uuid, result)
                    continue
                if result is None:
                    log.info("freenewsapi detail %s is no longer available", row.uuid)
                    continue
                articles.append(result)
                uuid_of[result.id] = row.uuid

        articles.sort(key=lambda article: article.published_at, reverse=True)
        return articles, uuid_of

    # ---------------------------------------------------------------- helpers

    async def _resolve(
        self,
        key: str,
        *,
        category: str | None,
        country: str | None,
        query: str | None,
        cursor: str | None,
    ) -> tuple[_Page, list[str], bool]:
        """Resolve one page under its own lock, returning `(page, providers, from_cache)`.

        The lock is what makes a burst cheap: two readers arriving on a cold
        page ask the providers once between them, not once each. The cache is
        re-read inside the lock so the second caller gets the first one's work.
        """
        async with self._lock_for(key):
            cached = self._cached_page(key)
            if cached is not None and cached[1]:
                return cached[0], [], True

            try:
                page, providers = await self._collect(
                    category=category, country=country, query=query, cursor=cursor
                )
            except ProviderError as exc:
                log.warning("all providers unavailable: %s", exc)
                # Last rung before an empty page: whatever we last served.
                if cached is not None:
                    return cached[0], [], True
                return _Page(), [], False

            self._db.cache_set(key, _encode(page), self._settings.cache_ttl_seconds)
            return page, providers, False

    def _renew_later(
        self,
        key: str,
        *,
        category: str | None,
        country: str | None,
        query: str | None,
        cursor: str | None,
    ) -> None:
        """Fetch a stale page again without making anyone wait for it."""
        if key in self._refreshing:
            return
        self._refreshing.add(key)

        async def renew() -> None:
            try:
                await self._resolve(
                    key, category=category, country=country, query=query, cursor=cursor
                )
            except Exception:  # a renewal must never take the process down
                log.warning("could not renew %s", key, exc_info=True)
            finally:
                self._refreshing.discard(key)

        task = asyncio.create_task(renew())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            if len(self._locks) > MAX_LOCKS:
                self._locks = {held: lock for held, lock in self._locks.items() if lock.locked()}
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _cached_page(self, key: str) -> tuple[_Page, bool] | None:
        """Return `(page, is_fresh)`, discarding an unreadable entry.

        A payload written before the page shape changed would otherwise raise
        on every request until its TTL lapsed, so it is dropped as a miss.
        """
        entry = self._db.cache_get(key)
        if entry is None:
            return None
        try:
            return _decode(entry[0]), entry[1]
        except ValueError:
            log.info("discarding unreadable cache entry %s", key)
            self._db.cache_delete(key)
            return None

    def require_client(self) -> httpx.AsyncClient:
        """The one shared upstream client, for callers making their own request.

        Public because the AI router needs it: summarising talks to a provider
        this aggregator does not otherwise know about, but it should still
        reuse the pooled connection rather than open a client per summary.
        """
        if self._client is None:
            raise ProviderError("HTTP client not started.", provider="all")
        return self._client

    def _api_key(self, provider: str) -> str:
        if provider == NEWSAPI:
            return self._settings.newsapi_key
        if provider == NEWSDATA:
            return self._settings.newsdata_key
        return self._settings.freenewsapi_key

    def _daily_limit(self, provider: str) -> int:
        if provider == NEWSAPI:
            return self._settings.newsapi_daily_limit
        if provider == NEWSDATA:
            return self._settings.newsdata_daily_limit
        return self._settings.freenewsapi_daily_limit

    def _available(self, provider: str) -> bool:
        return self._db.usage_used(provider) < self._daily_limit(provider)

    def _ready(self, provider: str) -> bool:
        if not self._api_key(provider):
            log.info("skipping %s: no api key configured", provider)
            return False
        if not self._available(provider):
            log.info("skipping %s: daily quota reached", provider)
            return False
        return True

    @staticmethod
    def _strip_cursor(cursor: str) -> str:
        return cursor[len(CURSOR_PREFIX) :] if cursor.startswith(CURSOR_PREFIX) else cursor

    def _quota(self, provider: str, limit: int) -> QuotaState:
        used = self._db.usage_used(provider)
        return QuotaState(
            provider=provider,
            used=used,
            limit=limit,
            remaining=max(limit - used, 0),
        )

    def _respond(
        self,
        page: _Page,
        *,
        limit: int,
        cached: bool,
        stale: bool,
        providers: list[str],
    ) -> FeedResponse:
        articles = page.articles[:limit]
        kept = {article.id for article in articles}
        return FeedResponse(
            articles=articles,
            meta=FeedMeta(
                count=len(articles),
                cached=cached,
                stale=stale,
                providers=providers,
                quotas=self.quotas(),
                next_cursor=page.next_cursor,
                has_more=page.has_more and page.next_cursor is not None,
                clusters=[story for story in page.clusters if story.lead_id in kept],
            ),
        )
