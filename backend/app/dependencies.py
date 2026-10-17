"""FastAPI dependencies backed by application state set in the lifespan."""

from __future__ import annotations

from fastapi import HTTPException, Request

from .db import Database
from .schemas import User
from .security import token_fingerprint
from .services import NewsAggregator

# The one cookie this app sets. Kept HttpOnly so no script can read the token.
SESSION_COOKIE = "athenaeum_session"


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_aggregator(request: Request) -> NewsAggregator:
    return request.app.state.aggregator


def get_current_user(request: Request) -> User | None:
    """Who is signed in, or None. Safe to call from any route."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return get_database(request).session_user(token_fingerprint(token))


def require_user(request: Request) -> User:
    """For routes that genuinely need an owner: bookmarks, follows, AI keys."""
    user = get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user
