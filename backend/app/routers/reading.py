"""What a reader has opened, so the app can offer to resume and dim the rest.

Kept per reader because "continue reading" is personal. The GET is deliberately
soft: signed out it answers with empty lists rather than a 401, since the feed
asks on every page load and an error there would be a console full of failures
on the most common path. Writing is the part that genuinely needs an owner.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..db import Database
from ..dependencies import get_current_user, get_database, require_user
from ..schemas import Article, ReadingEntry, ReadingPatch, ReadingResponse, User

router = APIRouter(prefix="/reading", tags=["reading"])


@router.get("", response_model=ReadingResponse)
async def list_reading(
    user: User | None = Depends(get_current_user),
    database: Database = Depends(get_database),
) -> ReadingResponse:
    """The resume rail plus the ids a feed should dim."""
    if user is None:
        return ReadingResponse()
    return ReadingResponse(
        entries=database.reading_list(user.id),
        read_ids=database.reading_read_ids(user.id),
    )


@router.post("", response_model=ReadingEntry, status_code=201)
async def record_open(
    article: Article,
    user: User = Depends(require_user),
    database: Database = Depends(get_database),
) -> ReadingEntry:
    """Note that a story was just opened, carrying the article with it.

    The article travels with the request rather than being looked up, because
    the feed page it came from may be evicted before the rail is next read.
    """
    return database.reading_record(user.id, article)


@router.patch("/{article_id}", response_model=ReadingEntry)
async def mark_read(
    article_id: str,
    patch: ReadingPatch,
    user: User = Depends(require_user),
    database: Database = Depends(get_database),
) -> ReadingEntry:
    """Mark a story read or unread. Opening one is the POST, not this."""
    entry = database.reading_set_read(user.id, article_id, patch.read)
    if entry is None:
        raise HTTPException(status_code=404, detail="That story is not in your reading history.")
    return entry
