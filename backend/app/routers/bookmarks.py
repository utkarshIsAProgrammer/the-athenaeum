from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from ..db import Database
from ..dependencies import get_database, require_user
from ..schemas import Article, Bookmark, BookmarksResponse, User

router = APIRouter(prefix="/bookmarks", tags=["bookmarks"])


@router.get("", response_model=BookmarksResponse)
async def list_bookmarks(
    user: User = Depends(require_user), database: Database = Depends(get_database)
) -> BookmarksResponse:
    return BookmarksResponse(bookmarks=database.bookmarks_list(user.id))


@router.post("", response_model=Bookmark, status_code=201)
async def create_bookmark(
    article: Article,
    user: User = Depends(require_user),
    database: Database = Depends(get_database),
) -> Bookmark:
    """Persist a whole article against the signed-in reader.

    Bookmarks are self-contained rather than a foreign key into a shared
    article table, because cached feed pages are evicted independently and a
    saved story must survive that.
    """
    return database.bookmark_add(user.id, article)


@router.delete("/{article_id}", status_code=204)
async def delete_bookmark(
    article_id: str,
    user: User = Depends(require_user),
    database: Database = Depends(get_database),
) -> Response:
    if not database.bookmark_delete(user.id, article_id):
        raise HTTPException(status_code=404, detail="Bookmark not found.")
    return Response(status_code=204)
