"""Registration, sign-in and sign-out.

Sessions are database rows rather than signed tokens: it means a sign-out is a
real revocation, not just a cookie the browser was asked to forget.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..config import Settings, get_settings
from ..db import Database
from ..dependencies import SESSION_COOKIE, get_current_user, get_database
from ..schemas import Credentials, MeResponse, User
from ..security import hash_password, new_session_token, token_fingerprint, verify_password

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _start_session(response: Response, database: Database, settings: Settings, user_id: str) -> User:
    """Mint a session, set the cookie and hand back the reader."""
    token = new_session_token()
    database.session_create(token_fingerprint(token), user_id, settings.session_ttl_seconds)

    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )

    user = database.user_get(user_id)
    if user is None:  # pragma: no cover - the row was just written
        raise HTTPException(status_code=500, detail="Could not start a session.")
    return user


@router.post("/register", response_model=User, status_code=201)
async def register(
    credentials: Credentials,
    response: Response,
    database: Database = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> User:
    email = credentials.email.strip()
    try:
        user = database.user_create(email, hash_password(credentials.password))
    except sqlite3.IntegrityError:
        # The unique index on lower(email) is the only thing that can raise here.
        raise HTTPException(status_code=409, detail="That email is already registered.") from None

    log.info("registered %s", user.id)
    return _start_session(response, database, settings, user.id)


@router.post("/login", response_model=User)
async def login(
    credentials: Credentials,
    response: Response,
    database: Database = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> User:
    found = database.credentials_by_email(credentials.email.strip())
    # Verify even when the account is missing, so a wrong email and a wrong
    # password take the same time and cannot be told apart.
    stored = found[1] if found else hash_password("no-such-account")
    matches = verify_password(credentials.password, stored)

    if found is None or not matches:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")

    return _start_session(response, database, settings, found[0].id)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    database: Database = Depends(get_database),
) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        database.session_delete(token_fingerprint(token))

    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me", response_model=MeResponse)
async def me(user: User | None = Depends(get_current_user)) -> MeResponse:
    """Signed out is a 200 with a null user, not an error."""
    return MeResponse(user=user)
