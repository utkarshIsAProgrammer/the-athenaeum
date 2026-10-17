from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..dependencies import get_aggregator, get_client_key, get_current_user, search_limit_exceeded
from ..schemas import FeedResponse
from ..services.aggregator import (
    CATEGORIES,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    NewsAggregator,
    SearchQuotaExceeded,
)

router = APIRouter(tags=["feed"])

VALID_CATEGORIES = {slug for slug, _label in CATEGORIES}


@router.get("/feed", response_model=FeedResponse)
async def read_feed(
    request: Request,
    category: str | None = Query(default=None, description="Category slug, e.g. `technology`."),
    country: str | None = Query(default=None, min_length=2, max_length=2, description="ISO country code."),
    q: str | None = Query(default=None, min_length=2, max_length=200, description="Optional keyword filter."),
    limit: int = Query(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT),
    cursor: str | None = Query(
        default=None,
        max_length=1024,
        description="Opaque page token from a previous response's `meta.next_cursor`.",
    ),
    news: NewsAggregator = Depends(get_aggregator),
) -> FeedResponse:
    if category is not None and category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown category '{category}'. Valid: {', '.join(sorted(VALID_CATEGORIES))}.",
        )

    try:
        return await news.feed(
            category=category,
            country=country.lower() if country else None,
            query=q.strip() if q else None,
            limit=limit,
            cursor=cursor,
            # A keyword filter is the same upstream call as `/api/search`, so it
            # is metered by the same allowance rather than a second door into it.
            client=get_client_key(request),
            signed_in=get_current_user(request) is not None,
        )
    except SearchQuotaExceeded as exc:
        raise search_limit_exceeded(exc) from exc
