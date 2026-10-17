"""Aggregator behaviour: merging, the degradation ladder, paging, hydration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db import Database
from app.providers import freenewsapi, newsapi, newsdata
from app.providers.base import ProviderError
from app.providers.freenewsapi import Listing
from app.schemas import Article, ArticleDetail
from app.services import reader
from app.services.aggregator import (
    CURSOR_PREFIX,
    MAX_BUDGET_CLIENTS,
    NewsAggregator,
    SearchBudget,
    SearchQuotaExceeded,
)

WHEN = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "newsapi_key": "newsapi-key",
        "newsdata_key": "newsdata-key",
        "freenewsapi_key": "freenewsapi-key",
        "database_path": str(tmp_path / "test.db"),
        # A body is fetched once and cached permanently, so the pacing can be
        # ignored in tests; the limiter has its own coverage.
        "freenewsapi_requests_per_second": 1000.0,
    }
    values.update(overrides)
    return Settings(**values)


def article(
    *,
    article_id: str,
    title: str = "A story about something happening",
    provider: str = "newsdata",
    image: bool = False,
    description: str | None = "A short summary.",
    source: str = "Example News",
) -> Article:
    return Article(
        id=article_id,
        title=title,
        description=description,
        url=f"https://example.com/{article_id}",
        image_url="https://img/photo.jpg" if image else None,
        source=source,
        published_at=WHEN,
        provider=provider,
    )


def detail(*, uuid: str, article_id: str, title: str, body: str = "Full text. " * 40) -> ArticleDetail:
    base = article(article_id=article_id, title=title, provider="freenewsapi", image=True)
    return ArticleDetail(**base.model_dump(), body=body, incipit="In brief.")


@pytest.fixture
async def aggregator(tmp_path: Path):
    settings = make_settings(tmp_path)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = NewsAggregator(settings, database)
    await news.startup()
    yield news
    await news.shutdown()
    database.close()


def stub_providers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    newsdata_articles: list[Article] | None = None,
    newsapi_articles: list[Article] | None = None,
    listing: list[Listing] | None = None,
    details: dict[str, ArticleDetail | None] | None = None,
    calls: list[str] | None = None,
    delay: float = 0.0,
) -> None:
    # `delay` gives the stub a suspension point, so several callers can be
    # inside the same fetch at once — which is the only way to observe whether
    # a burst reaches the providers once or once each.
    async def fake_newsdata(*_args: Any, **_kwargs: Any) -> tuple[list[Article], None]:
        if calls is not None:
            calls.append("newsdata")
        if delay:
            await asyncio.sleep(delay)
        return list(newsdata_articles or []), None

    async def fake_newsapi(*_args: Any, **_kwargs: Any) -> list[Article]:
        if calls is not None:
            calls.append("newsapi")
        if delay:
            await asyncio.sleep(delay)
        return list(newsapi_articles or [])

    async def fake_listing(*_args: Any, **_kwargs: Any) -> tuple[list[Listing], str]:
        if calls is not None:
            calls.append("listing")
        if delay:
            await asyncio.sleep(delay)
        return list(listing or []), "TOKEN-2"

    async def fake_details(*_args: Any, **_kwargs: Any) -> ArticleDetail | None:
        uuid = _args[3] if len(_args) > 3 else _kwargs.get("uuid")
        if calls is not None:
            calls.append(f"detail:{uuid}")
        if delay:
            await asyncio.sleep(delay)
        return (details or {}).get(uuid)

    monkeypatch.setattr(newsdata, "fetch", fake_newsdata)
    monkeypatch.setattr(newsapi, "fetch", fake_newsapi)
    monkeypatch.setattr(freenewsapi, "fetch_listing", fake_listing)
    monkeypatch.setattr(freenewsapi, "fetch_details", fake_details)


# --------------------------------------------------------------------- merging


async def test_the_same_url_from_two_providers_keeps_the_richer_copy(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = article(article_id="shared", provider="newsapi", image=False, description=None)
    illustrated = article(article_id="shared", provider="newsdata", image=True)
    stub_providers(
        monkeypatch, newsdata_articles=[illustrated], newsapi_articles=[plain]
    )

    feed = await aggregator.feed(category="technology", limit=20)

    assert len(feed.articles) == 1
    assert feed.articles[0].image_url is not None


async def test_the_first_page_offers_a_page_token_and_the_second_one_forwards_it(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_providers(monkeypatch, newsdata_articles=[article(article_id="one")])

    first = await aggregator.feed(category="technology", limit=20)
    assert first.meta.next_cursor == CURSOR_PREFIX
    assert first.meta.has_more is True

    stub_providers(
        monkeypatch,
        listing=[Listing(uuid="u1", title="A paged story", source="X", published_at=WHEN)],
        details={"u1": detail(uuid="u1", article_id="paged", title="A paged story")},
    )
    second = await aggregator.feed(category="technology", limit=20, cursor=first.meta.next_cursor)

    assert [item.id for item in second.articles] == ["paged"]
    assert second.meta.next_cursor == f"{CURSOR_PREFIX}TOKEN-2"
    assert second.meta.providers == ["freenewsapi"]


async def test_search_results_offer_no_cursor(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FreeNewsApi cannot search, so there is nothing to page into.
    stub_providers(monkeypatch, newsapi_articles=[article(article_id="found")])

    feed = await aggregator.feed(query="climate", limit=20)

    assert feed.meta.next_cursor is None
    assert feed.meta.has_more is False


# ---------------------------------------------------------------- degradation


async def test_an_empty_feed_is_returned_rather_than_an_error_when_every_provider_fails(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(*_args: Any, **_kwargs: Any) -> Any:
        raise ProviderError("upstream is down", provider="all")

    monkeypatch.setattr(newsdata, "fetch", failing)
    monkeypatch.setattr(newsapi, "fetch", failing)

    feed = await aggregator.feed(category="technology", limit=20)

    assert feed.articles == []
    assert feed.meta.count == 0
    assert len(feed.meta.quotas) == 3


async def test_the_last_good_page_is_served_once_the_providers_stop_answering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, cache_ttl_seconds=0)  # every entry is instantly stale
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = NewsAggregator(settings, database)
    await news.startup()
    try:
        stub_providers(monkeypatch, newsdata_articles=[article(article_id="kept")])
        first = await news.feed(category="technology", limit=20)
        assert first.meta.stale is False

        async def failing(*_args: Any, **_kwargs: Any) -> Any:
            raise ProviderError("upstream is down", provider="all")

        monkeypatch.setattr(newsdata, "fetch", failing)
        monkeypatch.setattr(newsapi, "fetch", failing)

        second = await news.feed(category="technology", limit=20)

        assert [item.id for item in second.articles] == ["kept"]
        assert second.meta.cached is True
        assert second.meta.stale is True
    finally:
        await news.shutdown()
        database.close()


async def test_a_provider_that_is_out_of_quota_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, newsdata_daily_limit=1)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = NewsAggregator(settings, database)
    await news.startup()
    calls: list[str] = []
    try:
        stub_providers(monkeypatch, newsdata_articles=[article(article_id="one")], calls=calls)
        await news.feed(category="technology", limit=20)
        assert "newsdata" in calls

        calls.clear()
        stub_providers(monkeypatch, newsapi_articles=[article(article_id="two")], calls=calls)
        await news.feed(category="sports", limit=20)

        assert "newsdata" not in calls
        assert "newsapi" in calls
    finally:
        await news.shutdown()
        database.close()


# ------------------------------------------------------------------- coalescing


async def test_a_burst_on_a_cold_page_reaches_the_providers_once(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole reason the free tiers survive: ten readers arriving on a page
    # nobody has asked for yet spend one request between them, not ten.
    calls: list[str] = []
    stub_providers(
        monkeypatch,
        newsdata_articles=[article(article_id="shared")],
        calls=calls,
        delay=0.05,
    )

    pages = await asyncio.gather(*(aggregator.feed(category="technology", limit=20) for _ in range(10)))

    assert calls.count("newsdata") == 1
    assert calls.count("newsapi") == 1
    assert all([item.id for item in page.articles] == ["shared"] for page in pages)


async def test_a_stale_page_is_renewed_once_however_many_readers_arrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An entry that never expires: every reader gets the stored copy, and the
    # renewal behind them is what must not multiply.
    settings = make_settings(tmp_path, cache_ttl_seconds=0)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = NewsAggregator(settings, database)
    await news.startup()
    calls: list[str] = []
    try:
        stub_providers(monkeypatch, newsdata_articles=[article(article_id="old")])
        await news.feed(category="technology", limit=20)

        calls.clear()
        stub_providers(
            monkeypatch,
            newsdata_articles=[article(article_id="new")],
            calls=calls,
            delay=0.05,
        )
        served = await asyncio.gather(
            *(news.feed(category="technology", limit=20) for _ in range(10))
        )

        assert all(page.meta.stale for page in served)
        # The renewals were scheduled rather than awaited; let them finish.
        await asyncio.gather(*list(news._tasks), return_exceptions=True)
        assert calls.count("newsdata") == 1
    finally:
        await news.shutdown()
        database.close()


# ------------------------------------------------------------------ hydration


async def test_a_row_that_cannot_be_hydrated_is_dropped_rather_than_shown(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A listing row has no link, so an article with no details would render as
    # a card pointing nowhere.
    stub_providers(
        monkeypatch,
        listing=[
            Listing(uuid="good", title="A story we can render", source="X", published_at=WHEN),
            Listing(uuid="gone", title="A story that aged out", source="Y", published_at=WHEN),
        ],
        details={"good": detail(uuid="good", article_id="good", title="A story we can render")},
    )

    feed = await aggregator.feed(category="technology", limit=20, cursor=f"{CURSOR_PREFIX}x")

    assert [item.id for item in feed.articles] == ["good"]


async def test_a_body_already_stored_is_not_fetched_twice(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    listing = [Listing(uuid="u1", title="A cached story here", source="X", published_at=WHEN)]
    stub_providers(
        monkeypatch,
        listing=listing,
        details={"u1": detail(uuid="u1", article_id="u1", title="A cached story here")},
        calls=calls,
    )

    await aggregator.feed(category="technology", limit=20, cursor=f"{CURSOR_PREFIX}a")
    calls.clear()
    # A different cursor, so the page cache cannot answer for us.
    await aggregator.feed(category="technology", limit=20, cursor=f"{CURSOR_PREFIX}b")

    assert "detail:u1" not in calls


async def test_an_article_served_by_a_feed_can_be_read_back_with_its_body(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = detail(uuid="u1", article_id="u1", title="A readable story here", body="Body text.")
    stub_providers(
        monkeypatch,
        listing=[Listing(uuid="u1", title="A readable story here", source="X", published_at=WHEN)],
        details={"u1": stored},
    )

    await aggregator.feed(category="technology", limit=20, cursor=f"{CURSOR_PREFIX}a")
    found = await aggregator.article("u1")

    assert found is not None
    assert found.body == "Body text."


async def test_an_unknown_article_is_reported_as_missing(aggregator: NewsAggregator) -> None:
    assert await aggregator.article("nope") is None


# --------------------------------------------------------------- body recovery

# What the reader is handed in place of a newsroom page in these tests.
RECOVERED = "The council approved the measure after a six-hour session. " * 10


def stub_reader(
    monkeypatch: pytest.MonkeyPatch, *, body: str | None, calls: list[str] | None = None
) -> None:
    async def fake_fetch_body(_client: Any, url: str, **_kwargs: Any) -> str | None:
        if calls is not None:
            calls.append(url)
        return body

    monkeypatch.setattr(reader, "fetch_body", fake_fetch_body)


async def test_a_story_no_provider_would_hand_over_is_read_at_its_publisher(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is the whole point of reader mode: NewsAPI and NewsData never give a
    # body, so without this a summary has nothing but a headline to work from.
    stub_providers(monkeypatch, newsdata_articles=[article(article_id="plain")])
    calls: list[str] = []
    stub_reader(monkeypatch, body=RECOVERED, calls=calls)

    await aggregator.feed(category="technology", limit=20)
    stored = await aggregator.article("plain", recover=False)
    assert stored is not None and stored.body is None, "the provider gave no body"

    found = await aggregator.article("plain")

    assert found is not None
    assert found.body == RECOVERED
    assert calls == ["https://example.com/plain"]

    # And it is kept: the second reader pays nothing at all.
    assert (await aggregator.article("plain")).body == RECOVERED
    assert calls == ["https://example.com/plain"]


async def test_a_publisher_that_yields_nothing_is_not_asked_again(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A paywall answers the same way for ever. Fetching it once per view is the
    # cost this avoids.
    stub_providers(monkeypatch, newsdata_articles=[article(article_id="walled")])
    calls: list[str] = []
    stub_reader(monkeypatch, body=None, calls=calls)

    await aggregator.feed(category="technology", limit=20)
    first = await aggregator.article("walled")
    second = await aggregator.article("walled")

    assert first is not None and first.body is None
    assert second is not None and second.body is None
    assert calls == ["https://example.com/walled"]


async def test_a_body_is_recovered_again_once_the_retry_window_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, reader_retry_seconds=0)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = NewsAggregator(settings, database)
    await news.startup()
    try:
        stub_providers(monkeypatch, newsdata_articles=[article(article_id="late")])
        calls: list[str] = []
        stub_reader(monkeypatch, body=None, calls=calls)
        await news.feed(category="technology", limit=20)

        await news.article("late")
        # The site was merely slow the first time; a second try finds it.
        stub_reader(monkeypatch, body=RECOVERED, calls=calls)
        found = await news.article("late")

        assert found is not None and found.body == RECOVERED
        assert calls == ["https://example.com/late", "https://example.com/late"]
    finally:
        await news.shutdown()
        database.close()


async def test_recovery_is_skipped_entirely_when_the_reader_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, reader_enabled=False)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = NewsAggregator(settings, database)
    await news.startup()
    try:
        stub_providers(monkeypatch, newsdata_articles=[article(article_id="plain")])
        calls: list[str] = []
        stub_reader(monkeypatch, body=RECOVERED, calls=calls)
        await news.feed(category="technology", limit=20)

        found = await news.article("plain")

        assert found is not None and found.body is None
        assert calls == []
    finally:
        await news.shutdown()
        database.close()


async def test_a_story_that_arrived_with_a_body_is_never_read_again(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_providers(
        monkeypatch,
        listing=[Listing(uuid="u1", title="A readable story here", source="X", published_at=WHEN)],
        details={"u1": detail(uuid="u1", article_id="u1", title="A readable story here")},
    )
    calls: list[str] = []
    stub_reader(monkeypatch, body=RECOVERED, calls=calls)

    await aggregator.feed(category="technology", limit=20, cursor=f"{CURSOR_PREFIX}a")
    found = await aggregator.article("u1")

    assert found is not None and found.body is not None
    assert calls == []


async def test_only_the_stored_copy_is_returned_when_recovery_is_not_wanted(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_providers(monkeypatch, newsdata_articles=[article(article_id="plain")])
    calls: list[str] = []
    stub_reader(monkeypatch, body=RECOVERED, calls=calls)
    await aggregator.feed(category="technology", limit=20)

    stored = await aggregator.article("plain", recover=False)

    assert stored is not None and stored.body is None
    assert calls == []


async def test_a_blocked_host_is_never_read(aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch) -> None:
    # Written straight to the table rather than fed, so what is under test is
    # the recovery step refusing the host — not the feed filter, which would
    # have dropped the row long before this.
    aggregator._db.articles_remember(
        [
            article(article_id="redirector", source="Google News").model_copy(
                update={"url": "https://news.google.com/rss/articles/xyz"}
            )
        ],
        uuid_of={},
    )
    calls: list[str] = []
    stub_reader(monkeypatch, body=RECOVERED, calls=calls)

    found = await aggregator.article("redirector")

    assert found is not None and found.body is None
    assert calls == []


# ------------------------------------------------------------------- trending


async def test_trending_surfaces_the_story_several_outlets_ran(
    aggregator: NewsAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_providers(
        monkeypatch,
        newsdata_articles=[
            article(
                article_id="lead",
                title="Tesla unveils the flying car Roadster",
                image=True,
                source="The Verge",
            )
        ],
        newsapi_articles=[
            article(
                article_id="also",
                title="Tesla's flying car Roadster stuns in demo",
                source="Reuters",
            )
        ],
    )

    trending = await aggregator.trending()

    assert [story.title for story in trending.stories] == [
        "Tesla unveils the flying car Roadster"
    ]
    assert len(trending.stories[0].sources) == 2


# --------------------------------------------------------------- search budget

# A search is the only input that costs an upstream call for every distinct
# value, so it is the one place a single client can spend the day's allowance
# for everyone. These cover both the counter and the refusal it produces.


def test_a_client_may_run_its_allowance_of_new_searches_and_no_more() -> None:
    budget = SearchBudget(anonymous_limit=2, signed_in_limit=5)

    assert budget.allow("a", signed_in=False) is True
    assert budget.allow("a", signed_in=False) is True
    assert budget.allow("a", signed_in=False) is False
    # Spending it does not spend anyone else's.
    assert budget.allow("b", signed_in=False) is True


def test_signing_in_raises_the_ceiling_the_same_client_is_counted_against() -> None:
    budget = SearchBudget(anonymous_limit=1, signed_in_limit=2)

    assert budget.allow("a", signed_in=False) is True
    assert budget.allow("a", signed_in=False) is False
    # What was spent anonymously still counts, but the ceiling is now the
    # larger one — so signing in is what the refusal told the reader to do.
    assert budget.allow("a", signed_in=True) is True
    assert budget.allow("a", signed_in=True) is False


def test_an_anonymous_allowance_of_zero_requires_an_account_to_search() -> None:
    budget = SearchBudget(anonymous_limit=0, signed_in_limit=10)

    assert budget.allow("a", signed_in=False) is False
    assert budget.allow("a", signed_in=True) is True


def test_the_budget_table_forgets_its_oldest_client_rather_than_growing() -> None:
    budget = SearchBudget(anonymous_limit=1, signed_in_limit=1)

    for index in range(MAX_BUDGET_CLIENTS + 100):
        budget.allow(f"client-{index}", signed_in=False)

    assert len(budget._used) <= MAX_BUDGET_CLIENTS


async def test_a_search_past_the_allowance_is_refused_rather_than_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, search_daily_queries_anonymous=2)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = NewsAggregator(settings, database)
    await news.startup()
    calls: list[str] = []
    stub_providers(monkeypatch, newsapi_articles=[article(article_id="found")], calls=calls)
    try:
        await news.feed(query="one", client="a", limit=20)
        await news.feed(query="two", client="a", limit=20)
        calls.clear()

        with pytest.raises(SearchQuotaExceeded) as refused:
            await news.feed(query="three", client="a", limit=20)

        assert refused.value.limit == 2
        assert refused.value.signed_in is False
        # And the refused call never reached a provider.
        assert calls == []
    finally:
        await news.shutdown()
        database.close()


async def test_a_repeated_search_is_answered_from_the_cache_and_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The allowance counts upstream calls, not requests: the same words typed
    # twice are one cache key, so the second search is free however little of
    # the allowance is left.
    settings = make_settings(tmp_path, search_daily_queries_anonymous=1)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = NewsAggregator(settings, database)
    await news.startup()
    calls: list[str] = []
    stub_providers(monkeypatch, newsapi_articles=[article(article_id="found")], calls=calls)
    try:
        first = await news.feed(query="climate", client="a", limit=20)
        calls.clear()

        # The allowance is spent now, so only a cache hit can answer this.
        again = await news.feed(query="climate", client="a", limit=20)

        assert first.meta.cached is False
        assert again.meta.cached is True
        assert calls == []
    finally:
        await news.shutdown()
        database.close()


async def test_browsing_the_feed_never_spends_a_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The allowance is only about `q`: a reader paging through categories with
    # the anonymous allowance set to zero must still get their feed.
    settings = make_settings(tmp_path, search_daily_queries_anonymous=0)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = NewsAggregator(settings, database)
    await news.startup()
    stub_providers(monkeypatch, newsdata_articles=[article(article_id="browsed")])
    try:
        feed = await news.feed(category="technology", client="a", limit=20)

        assert [item.id for item in feed.articles] == ["browsed"]
    finally:
        await news.shutdown()
        database.close()
