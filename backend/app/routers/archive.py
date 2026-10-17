"""Browsing the reading room by date.

This is an archive of what the app has shown, not of the news itself: the free
tiers cannot be asked for a day that has passed, so only stories some earlier
reader was actually served are here. That is a real limitation rather than a
bug, and the page says so instead of pretending.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import ARCHIVE_DAY_LIMIT, Database
from ..dependencies import get_database
from ..schemas import ArchiveDay, ArchiveDayResponse, ArchiveIndexResponse

router = APIRouter(prefix="/archive", tags=["archive"])

DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


@router.get("", response_model=ArchiveIndexResponse)
async def read_archive(database: Database = Depends(get_database)) -> ArchiveIndexResponse:
    """Which days the reading room holds stories for, newest first."""
    return ArchiveIndexResponse(
        days=[ArchiveDay(day=day, count=count) for day, count in database.archive_days()]
    )


@router.get("/{day}", response_model=ArchiveDayResponse)
async def read_day(
    day: str,
    limit: int = Query(default=50, ge=1, le=ARCHIVE_DAY_LIMIT),
    offset: int = Query(default=0, ge=0),
    database: Database = Depends(get_database),
) -> ArchiveDayResponse:
    """One day's stories, newest first.

    A day with nothing in it answers with an empty list rather than a 404 —
    the reader picked it from a list, so the page should be able to say what
    is missing rather than fail.
    """
    if not DAY.fullmatch(day):
        raise HTTPException(status_code=422, detail="A day is written as YYYY-MM-DD.")

    articles, total = database.archive_articles(day, limit=limit, offset=offset)
    return ArchiveDayResponse(day=day, articles=articles, total=total)
