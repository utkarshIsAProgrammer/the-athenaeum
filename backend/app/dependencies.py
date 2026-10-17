"""FastAPI dependencies backed by application state set in the lifespan."""

from __future__ import annotations

from fastapi import HTTPException, Request

from .db import Database
from .schemas import User
from .security import token_fingerprint
from .services import NewsAggregator
from .services.aggregator import SearchQuotaExceeded

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


def get_client_key(request: Request) -> str:
    """Who to count this request against.

    Decided once by `GuardsMiddleware`, which needs the same answer to do its
    own counting; the fallback only covers a request that arrived without the
    guards in front of it, as the tests build.
    """
    key = getattr(request.state, "client_key", "")
    return key or (request.client.host if request.client else "unknown")


def search_limit_exceeded(exc: SearchQuotaExceeded) -> HTTPException:
    """The refusal for a client that has spent the day's new searches.

    A search is the one call that reaches a provider for each distinct input,
    so an unlimited one lets a single client spend the whole day's allowance —
    and the feed then serves yesterday to everyone until the counters reset.
    The reply is a 429 rather than an empty page because the query is not
    wrong, it is merely too expensive to run again today.
    """
    if exc.signed_in:
        detail = (
            f"You have used all {exc.limit} of today's new searches. "
            "Searches you have already run keep working, and the allowance returns after midnight UTC."
        )
    else:
        detail = (
            f"You have used all {exc.limit} of today's new searches without an account. "
            "Sign in for a larger allowance, or try again after midnight UTC."
        )
    return HTTPException(status_code=429, detail=detail)
