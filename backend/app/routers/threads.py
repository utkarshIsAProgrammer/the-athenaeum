"""The other newsrooms that ran a story, as the clusterer saw them.

Answers softly with `thread: null` rather than a 404: the story page asks about
every article it renders, and most articles genuinely have no thread.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import Database
from ..dependencies import get_database
from ..schemas import ThreadResponse

router = APIRouter(prefix="/threads", tags=["threads"])


@router.get("/{article_id}", response_model=ThreadResponse)
async def read_thread(
    article_id: str, database: Database = Depends(get_database)
) -> ThreadResponse:
    """Every copy of this story that was folded together, or an empty answer.

    Resolved from the store rather than the feed cache: the page that first
    clustered the story is usually evicted long before the story is reopened.
    """
    return ThreadResponse(thread=database.thread_for(article_id))
