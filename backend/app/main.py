from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import Database
from .errors import install_error_handlers
from .middleware import GuardsMiddleware, SecurityHeadersMiddleware
from .providers.base import FREENEWSAPI, NEWSAPI, NEWSDATA
from .routers import (
    ai,
    archive,
    articles,
    auth,
    bookmarks,
    feed,
    follows,
    meta,
    reading,
    search,
    threads,
    trending,
)
from .services import NewsAggregator

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    aggregator = NewsAggregator(settings, database)
    await aggregator.startup()

    app.state.settings = settings
    app.state.database = database
    app.state.aggregator = aggregator

    missing = [
        name
        for name, key in (
            (NEWSAPI, settings.newsapi_key),
            (NEWSDATA, settings.newsdata_key),
            (FREENEWSAPI, settings.freenewsapi_key),
        )
        if not key
    ]
    if missing:
        log.warning("no API key configured for: %s", ", ".join(missing))

    # Not a startup error, because an API on a private network is reached only
    # by the proxy and has nothing to guard against. On a host that anyone can
    # resolve it is the difference between a quota and an open tap, and it is
    # the kind of omission that is invisible until the feed runs dry.
    if settings.environment == "production" and not settings.internal_api_secret:
        log.warning(
            "no INTERNAL_API_SECRET: anyone who can reach this service can pose as a new "
            "client on every request and spend the day's provider allowance"
        )

    log.info("database  %s", settings.database_file)
    log.info("cache TTL %ss | stale window %ss", settings.cache_ttl_seconds, settings.stale_cache_ttl_seconds)

    try:
        yield
    finally:
        await aggregator.shutdown()
        database.close()


settings = get_settings()

app = FastAPI(
    title="The Athenaeum API",
    version="0.2.0",
    summary="A cached, de-duplicated feed merging NewsAPI.org, NewsData.io and FreeNewsApi.io.",
    lifespan=lifespan,
)

# Starlette builds the stack in reverse: the last one added is the outermost,
# so the order below is innermost-to-outermost. CORS stays next to the routes
# because a preflight has to be answered even for a request the guards would
# count, and the security headers go on the very outside so they are present on
# a refusal the guards emit themselves.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.add_middleware(
    GuardsMiddleware,
    settings=settings,
)

# `session_cookie_secure` is the operator's statement that this deployment is
# served over TLS, which is exactly the condition HSTS must wait for.
app.add_middleware(
    SecurityHeadersMiddleware,
    hsts=settings.session_cookie_secure,
)

install_error_handlers(app)

api = APIRouter(prefix="/api")
api.include_router(auth.router)
api.include_router(ai.router)
api.include_router(feed.router)
api.include_router(search.router)
api.include_router(trending.router)
api.include_router(articles.router)
api.include_router(bookmarks.router)
api.include_router(reading.router)
api.include_router(follows.router)
api.include_router(threads.router)
api.include_router(archive.router)
api.include_router(meta.router)
app.include_router(api)


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
