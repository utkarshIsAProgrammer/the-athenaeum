from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..dependencies import get_aggregator
from ..schemas import TrendingResponse
from ..services import NewsAggregator

router = APIRouter(tags=["trending"])


@router.get("/trending", response_model=TrendingResponse)
async def read_trending(
    limit: int = Query(default=12, ge=1, le=30, description="How many clustered stories to return."),
    news: NewsAggregator = Depends(get_aggregator),
) -> TrendingResponse:
    """Stories ranked by how many outlets covered them, plus busiest subjects.

    Built from the cached top-stories page, so it costs no extra provider
    quota once the front page has been loaded.
    """
    return await news.trending(limit=limit)
