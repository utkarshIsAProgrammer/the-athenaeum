"""Registration, sign-in, sign-out, recovery and session lifetime.

Sessions are database rows rather than signed tokens: it means a sign-out is a
real revocation, not just a cookie the browser was asked to forget.

There is no password-reset email, and there will not be one. Recovery works the
other way round: the reader is handed a code to write down while they are still
signed in, and that code is the only thing that can open the account again if
the password is lost. It is a second password, stored and spent like one.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..config import Settings, get_settings
from ..db import Database
from ..dependencies import SESSION_COOKIE, get_current_user, get_database, require_user
from ..schemas import (
    Credentials,
    MeResponse,
    RecoveryCode,
    RecoveryRequest,
    User,
)
from ..security import (
    hash_password,
    new_recovery_code,
    new_session_token,
    normalise_recovery_code,
    token_fingerprint,
    verify_password,
)

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


@router.post("/recovery-code", response_model=RecoveryCode, status_code=201)
async def issue_recovery_code(
    user: User = Depends(require_user),
    database: Database = Depends(get_database),
) -> RecoveryCode:
    """Mint a recovery code and hand it over, once.

    This is the whole way back into an account whose password is lost, since
    nothing is ever emailed, so it can only be handed out to someone who is
    already signed in — a reader asking for one after they have been locked out
    is asking too late, and that is the cost of having no mail service.

    Only the hash is kept. Asking again replaces the previous code, because a
    code written down twice is a code kept in two places, and the older one is
    the one that was more likely to leak.
    """
    code = new_recovery_code()
    database.user_recovery_set(user.id, hash_password(normalise_recovery_code(code)))
    log.info("issued a recovery code to %s", user.id)
    return RecoveryCode(recovery_code=code)


@router.post("/recover", status_code=204)
async def recover(
    payload: RecoveryRequest,
    database: Database = Depends(get_database),
) -> Response:
    """Set a new password with a recovery code, and sign every session out.

    The code is spent in the act: a written-down code that keeps working is a
    password left in a drawer, so the account page issues a fresh one after the
    reader is back in. Every session goes too, for the same reason a password
    change does — whoever recovered the account is the only person who has
    proved they own it.
    """
    found = database.recovery_by_email(payload.email.strip())
    # Verify even when there is no account or no code, so a wrong address is
    # answered as slowly as a wrong code and neither can be told from the other.
    stored = found[1] if found and found[1] else hash_password("no-such-account")
    matches = verify_password(normalise_recovery_code(payload.recovery_code), stored)

    if found is None or not matches:
        log.info("rejected a recovery attempt")
        raise HTTPException(status_code=401, detail="That email and recovery code do not match.")

    user_id = found[0].id
    database.user_password_set(user_id, hash_password(payload.password))
    database.user_recovery_set(user_id, None)
    revoked = database.sessions_delete_for_user(user_id)
    log.info("recovered %s with a recovery code; %d session(s) revoked", user_id, revoked)

    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
