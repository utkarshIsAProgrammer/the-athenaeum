from __future__ import annotations

from fastapi import APIRouter, Request

from ..config import get_settings
from ..providers.base import FREENEWSAPI, NEWSAPI, NEWSDATA
from ..schemas import CategoriesResponse, Category, HealthResponse
from ..services.aggregator import CATEGORIES

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings = get_settings()
    aggregator = getattr(request.app.state, "aggregator", None)

    configured = [
        name
        for name, key in (
            (NEWSAPI, settings.newsapi_key),
            (NEWSDATA, settings.newsdata_key),
            (FREENEWSAPI, settings.freenewsapi_key),
        )
        if key
    ]

    return HealthResponse(
        ok=True,
        service="athenaeum-news-api",
        providers=configured,
        quotas=aggregator.quotas() if aggregator is not None else [],
    )


@router.get("/categories", response_model=CategoriesResponse)
async def categories() -> CategoriesResponse:
    return CategoriesResponse(
        categories=[Category(slug=slug, label=label) for slug, label in CATEGORIES]
    )
