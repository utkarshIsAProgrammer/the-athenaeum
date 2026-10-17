"""The refusal `/api/search` gives once a client's daily allowance is spent.

The aggregator does the refusing; what matters to the reader is what comes
back, because a 429 that does not say what to do next is its own bug — and the
two messages are different on purpose, since one reader can raise the ceiling
and the other cannot.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import Database
from app.dependencies import get_aggregator, get_database
from app.errors import install_error_handlers
from app.routers import search
from app.services.aggregator import SearchQuotaExceeded


class SpentNews:
    """An aggregator whose caller has already spent the day's searches.

    It records what the route handed it, so the request can be checked to have
    been attributed to the right client rather than to nobody.
    """

    def __init__(self, *, signed_in: bool, limit: int) -> None:
        self.signed_in = signed_in
        self.limit = limit
        self.seen: dict[str, Any] = {}

    async def feed(self, **kwargs: Any):
        self.seen = kwargs
        raise SearchQuotaExceeded(self.limit, signed_in=self.signed_in)


@pytest.fixture
def env(tmp_path: Path):
    settings = Settings(
        database_path=str(tmp_path / "unused.db"),
        newsapi_key="newsapi-key",
        newsdata_key="newsdata-key",
        freenewsapi_key="freenewsapi-key",
    )
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = SpentNews(signed_in=False, limit=settings.search_daily_queries_anonymous)

    app = FastAPI()
    install_error_handlers(app)
    api = APIRouter(prefix="/api")
    api.include_router(search.router)
    app.include_router(api)

    app.state.settings = settings
    app.state.database = database
    app.state.aggregator = news
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_aggregator] = lambda: news

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, news=news, settings=settings)

    database.close()


def test_a_signed_out_reader_is_told_that_signing_in_is_what_raises_it(env) -> None:
    refused = env.client.get("/api/search", params={"q": "climate"})

    assert refused.status_code == 429
    error = refused.json()["error"]
    assert str(env.settings.search_daily_queries_anonymous) in error
    assert "without an account" in error
    assert "Sign in" in error


def test_a_signed_in_reader_is_given_the_count_and_not_told_to_sign_in(env) -> None:
    env.news.signed_in = True
    env.news.limit = 50

    refused = env.client.get("/api/search", params={"q": "climate"})

    assert refused.status_code == 429
    error = refused.json()["error"]
    assert "50" in error
    # The advice would be useless, since they already are.
    assert "sign in" not in error.lower()


def test_the_search_is_attributed_to_the_calling_client(env) -> None:
    env.client.get("/api/search", params={"q": "wind farms"})

    # Whitespace is stripped before the query becomes a cache key, and the
    # client the guards counted is what it is metered against.
    assert env.news.seen["query"] == "wind farms"
    assert env.news.seen["client"] == "testclient"
    assert env.news.seen["signed_in"] is False
