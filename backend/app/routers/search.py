from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..dependencies import get_aggregator, get_client_key, get_current_user, search_limit_exceeded
from ..schemas import FeedResponse
from ..services.aggregator import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    NewsAggregator,
    SearchQuotaExceeded,
)

router = APIRouter(tags=["search"])


@router.get("/search", response_model=FeedResponse)
async def search_articles(
    request: Request,
    q: str = Query(min_length=2, max_length=200, description="Keyword search."),
    country: str | None = Query(default=None, min_length=2, max_length=2),
    limit: int = Query(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    news: NewsAggregator = Depends(get_aggregator),
) -> FeedResponse:
    term = q.strip()
    if len(term) < 2:
        raise HTTPException(
            status_code=422,
            detail="Search query must be at least 2 characters.",
        )

    try:
        return await news.feed(
            query=term,
            country=country.lower() if country else None,
            limit=limit,
            client=get_client_key(request),
            signed_in=get_current_user(request) is not None,
        )
    except SearchQuotaExceeded as exc:
        raise search_limit_exceeded(exc) from exc
