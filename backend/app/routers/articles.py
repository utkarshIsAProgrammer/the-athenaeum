from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..dependencies import get_aggregator
from ..schemas import ArticleDetail
from ..services import NewsAggregator

router = APIRouter(prefix="/articles", tags=["articles"])


@router.get("/{article_id}", response_model=ArticleDetail)
async def read_article(
    article_id: str, news: NewsAggregator = Depends(get_aggregator)
) -> ArticleDetail:
    """Reader mode: one article with its body.

    Every article a feed has served is recorded, but only FreeNewsApi supplies
    a body. For the rest the publisher's own page is read once, here, and the
    text kept — so the reader gets the story rather than a standfirst, and a
    summary has something to condense. `body` is still null when that page
    could not be read (paywall, bot wall, JavaScript-only), which is not an
    error: the reader falls back to the summary and the link out.
    """
    article = await news.article(article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="That article is no longer available.")
    return article
