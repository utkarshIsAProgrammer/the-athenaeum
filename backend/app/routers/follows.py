"""Subjects and outlets a reader asked to see more of.

The GET is deliberately soft — empty lists rather than a 401 when signed out —
because the story page asks on every load so it can show which of a story's
subjects are already followed. An error there would be a console full of
failures on the most common path. Writing is the part that needs an owner.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..db import Database
from ..dependencies import get_current_user, get_database, require_user
from ..schemas import Follow, FollowInput, FollowKind, FollowsResponse, User
from ..services.aggregator import CATEGORY_LABELS

router = APIRouter(prefix="/follows", tags=["follows"])

# "top" is a placement on the front page, not a subject, so following it would
# mean nothing — the reader is already there.
FOLLOWABLE_TOPICS = frozenset(CATEGORY_LABELS) - {"top"}


def _follow(kind: str, value: str) -> Follow:
    """Resolve a stored follow into the shape the client renders."""
    label = CATEGORY_LABELS.get(value, value) if kind == "topic" else value
    return Follow(kind=kind, value=value, label=label)


def _validate(follow: FollowInput) -> str:
    """Return the trimmed value, rejecting anything the feed cannot answer."""
    value = follow.value.strip()
    if not value:
        raise HTTPException(status_code=422, detail="A follow needs a name.")

    if follow.kind == "topic" and value not in FOLLOWABLE_TOPICS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown topic '{value}'. "
                f"Valid: {', '.join(sorted(FOLLOWABLE_TOPICS))}."
            ),
        )
    return value


@router.get("", response_model=FollowsResponse)
async def list_follows(
    user: User | None = Depends(get_current_user),
    database: Database = Depends(get_database),
) -> FollowsResponse:
    """What this reader follows, newest first."""
    if user is None:
        return FollowsResponse()
    return FollowsResponse(
        follows=[_follow(kind, value) for kind, value in database.follows_list(user.id)]
    )


@router.post("", response_model=Follow, status_code=201)
async def add_follow(
    follow: FollowInput,
    user: User = Depends(require_user),
    database: Database = Depends(get_database),
) -> Follow:
    """Follow a subject or an outlet. Following something twice is a no-op."""
    value = _validate(follow)
    database.follow_add(user.id, follow.kind, value)
    return _follow(follow.kind, value)


@router.delete("", status_code=204)
async def remove_follow(
    kind: FollowKind,
    # A query rather than a path segment: outlet names contain spaces and
    # slashes ("BBC News/Sport"), which no path parameter survives intact.
    value: str = Query(min_length=1, max_length=120),
    user: User = Depends(require_user),
    database: Database = Depends(get_database),
) -> Response:
    """Unfollow. Nothing to unfollow is not an error — this is a toggle."""
    database.follow_delete(user.id, kind, value.strip())
    return Response(status_code=204)
