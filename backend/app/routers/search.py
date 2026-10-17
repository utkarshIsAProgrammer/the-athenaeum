from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..dependencies import get_aggregator
from ..schemas import FeedResponse
from ..services.aggregator import DEFAULT_LIMIT, MAX_LIMIT, MIN_LIMIT, NewsAggregator

router = APIRouter(tags=["search"])


@router.get("/search", response_model=FeedResponse)
async def search_articles(
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

    return await news.feed(
        query=term,
        country=country.lower() if country else None,
        limit=limit,
    )
