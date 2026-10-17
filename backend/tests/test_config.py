"""`ENVIRONMENT=production` is a claim about the deployment, not a switch.

Every omission the check catches is one that would fail quietly rather than
loudly — a session cookie sent over plaintext, provider keys encrypted with
nothing — so it has to stop the process instead of warning. Development keeps
the same defaults and must never be stopped by them.
"""

from __future__ import annotations

import pytest

from app.config import Settings

# A deployment that is actually finished, as the check defines one.
PRODUCTION: dict[str, object] = {
    "environment": "production",
    "session_cookie_secure": True,
    "secret_key": "a-generated-secret-worth-keeping",
}


def production(**overrides: object) -> Settings:
    return Settings(**{**PRODUCTION, **overrides})


def test_a_complete_production_config_is_accepted() -> None:
    assert production().environment == "production"


def test_production_refuses_a_session_cookie_that_would_travel_in_the_clear() -> None:
    with pytest.raises(ValueError, match="SESSION_COOKIE_SECURE"):
        production(session_cookie_secure=False)


def test_production_refuses_to_start_without_a_secret_for_readers_keys() -> None:
    # Without one there is nothing to encrypt a reader's AI key with, so saving
    # one silently stops working — better to be told now.
    with pytest.raises(ValueError, match="SECRET_KEY"):
        production(secret_key="")


def test_every_missing_piece_is_reported_at_once() -> None:
    # Being stopped by the next omission each time is a slow way to find out
    # what the list was.
    with pytest.raises(ValueError) as refused:
        Settings(
            environment="production",
            session_cookie_secure=False,
            secret_key="",
        )

    message = str(refused.value)
    assert "SESSION_COOKIE_SECURE" in message
    assert "SECRET_KEY" in message


def test_development_is_left_alone() -> None:
    # This is the shape every developer's `.env` is in, and the check exists
    # only for the environment where those values are wrong rather than normal.
    settings = Settings(
        environment="development",
        session_cookie_secure=False,
        secret_key="",
    )

    assert settings.environment == "development"
