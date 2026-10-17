"""Accounts: registration, sign-in, session lifetime and per-reader bookmarks."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import Database
from app.dependencies import get_database
from app.errors import install_error_handlers
from app.routers import auth, bookmarks
from app.security import hash_password, new_session_token, token_fingerprint, verify_password

WHEN = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

READER = {"email": "reader@example.com", "password": "correct horse battery"}
OTHER = {"email": "other@example.com", "password": "a different passphrase"}


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "newsapi_key": "newsapi-key",
        "newsdata_key": "newsdata-key",
        "freenewsapi_key": "freenewsapi-key",
        "database_path": str(tmp_path / "test.db"),
    }
    values.update(overrides)
    return Settings(**values)


def article(article_id: str = "article-1", title: str = "A story about something") -> dict[str, Any]:
    return {
        "id": article_id,
        "title": title,
        "description": "A short summary.",
        "url": f"https://example.com/{article_id}",
        "image_url": None,
        "source": "Example News",
        "published_at": WHEN.isoformat(),
        "provider": "newsdata",
    }


@pytest.fixture
def client(tmp_path: Path):
    """A minimal app carrying only the routers under test.

    `app.main` is deliberately avoided: its lifespan opens the real database
    from `.env` and starts the aggregator, so a test would depend on the
    developer's keys.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)

    app = FastAPI()
    install_error_handlers(app)
    api = APIRouter(prefix="/api")
    api.include_router(auth.router)
    api.include_router(bookmarks.router)
    app.include_router(api)

    app.state.settings = settings
    app.state.database = database
    # `get_settings` is `lru_cache`d and `get_database` reads `app.state`, so
    # overrides are the only way to keep the fixture's temporary database.
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_database] = lambda: database

    with TestClient(app) as test_client:
        yield test_client

    database.close()


def register(client: TestClient, credentials: dict[str, str] = READER):
    return client.post("/api/auth/register", json=credentials)


# ----------------------------------------------------------------- security


def test_password_hash_round_trips_and_is_salted() -> None:
    first = hash_password("correct horse battery")
    second = hash_password("correct horse battery")

    assert first != second, "a shared salt would let one rainbow table crack every row"
    assert verify_password("correct horse battery", first)
    assert not verify_password("Correct horse battery", first)


def test_verify_rejects_a_malformed_hash() -> None:
    # A truncated or hand-edited column must read as "wrong password", not a 500.
    assert not verify_password("anything", "not-a-hash")
    assert not verify_password("anything", "md5$1$1$1$c2FsdA$aGFzaA")


def test_token_fingerprint_is_stable_and_not_the_token() -> None:
    token = new_session_token()
    assert token_fingerprint(token) == token_fingerprint(token)
    assert token_fingerprint(token) != token
    assert token_fingerprint(token) != token_fingerprint(new_session_token())


# ----------------------------------------------------------------- register


def test_register_starts_a_session(client: TestClient) -> None:
    response = register(client)

    assert response.status_code == 201
    assert response.json()["email"] == READER["email"]
    assert "password" not in response.text, "the hash must never reach the client"
    assert client.cookies.get("athenaeum_session")

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == READER["email"]


def test_register_rejects_a_duplicate_email(client: TestClient) -> None:
    assert register(client).status_code == 201

    response = register(client)
    assert response.status_code == 409
    assert response.json()["error"] == "That email is already registered."


def test_register_treats_email_case_insensitively(client: TestClient) -> None:
    register(client)

    response = client.post(
        "/api/auth/register",
        json={**READER, "email": READER["email"].upper()},
    )
    assert response.status_code == 409


def test_register_rejects_a_short_password(client: TestClient) -> None:
    response = client.post("/api/auth/register", json={**READER, "password": "short"})
    assert response.status_code == 422
    assert "password" in response.json()["error"]


# -------------------------------------------------------------------- login


def test_me_is_null_rather_than_an_error_when_signed_out(client: TestClient) -> None:
    response = client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.json() == {"user": None}


def test_login_accepts_the_right_password(client: TestClient) -> None:
    register(client)
    client.cookies.clear()

    response = client.post("/api/auth/login", json=READER)
    assert response.status_code == 200
    assert client.get("/api/auth/me").json()["user"]["email"] == READER["email"]


def test_login_rejects_a_wrong_password(client: TestClient) -> None:
    register(client)
    client.cookies.clear()

    response = client.post("/api/auth/login", json={**READER, "password": "not it at all"})
    assert response.status_code == 401
    assert response.json()["error"] == "Email or password is incorrect."


def test_login_rejects_an_unknown_email_the_same_way(client: TestClient) -> None:
    # One message for both cases, so the endpoint cannot be used to discover
    # which addresses have accounts.
    response = client.post("/api/auth/login", json={**READER, "email": "nobody@example.com"})
    assert response.status_code == 401
    assert response.json()["error"] == "Email or password is incorrect."


def test_logout_revokes_the_session_server_side(client: TestClient) -> None:
    register(client)
    stolen = client.cookies.get("athenaeum_session")

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").json() == {"user": None}

    # A copied cookie must be dead too: the row is gone, not just the browser's.
    client.cookies.set("athenaeum_session", stolen)
    assert client.get("/api/auth/me").json() == {"user": None}


def test_an_expired_session_is_refused(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    user = database.user_create(READER["email"], hash_password(READER["password"]))

    token = new_session_token()
    fingerprint = token_fingerprint(token)
    database.session_create(fingerprint, user.id, ttl_seconds=-1)

    assert database.session_user(fingerprint) is None
    # And the dead row was swept, rather than lingering forever.
    rows = database._conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
    assert rows["n"] == 0
    database.close()


# ---------------------------------------------------------------- bookmarks


def test_bookmarks_require_an_account(client: TestClient) -> None:
    assert client.get("/api/bookmarks").status_code == 401
    assert client.post("/api/bookmarks", json=article()).status_code == 401
    assert client.delete("/api/bookmarks/article-1").status_code == 401

    body = client.get("/api/bookmarks").json()
    assert body["error"] == "Sign in to continue."


def test_bookmarks_are_scoped_per_reader(client: TestClient) -> None:
    register(client, READER)
    created = client.post("/api/bookmarks", json=article("article-mine"))
    assert created.status_code == 201
    assert created.json()["bookmarked_at"]

    client.post("/api/auth/logout")
    register(client, OTHER)
    assert client.get("/api/bookmarks").json()["bookmarks"] == []

    client.post("/api/bookmarks", json=article("article-theirs"))
    theirs = [row["id"] for row in client.get("/api/bookmarks").json()["bookmarks"]]
    assert theirs == ["article-theirs"]

    client.post("/api/auth/logout")
    client.post("/api/auth/login", json=READER)
    mine_back = [row["id"] for row in client.get("/api/bookmarks").json()["bookmarks"]]
    assert mine_back == ["article-mine"]


def test_saving_the_same_story_twice_is_idempotent(client: TestClient) -> None:
    register(client)

    first = client.post("/api/bookmarks", json=article("article-1"))
    second = client.post("/api/bookmarks", json=article("article-1"))

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["bookmarked_at"] == second.json()["bookmarked_at"]
    assert len(client.get("/api/bookmarks").json()["bookmarks"]) == 1


def test_deleting_an_unsaved_story_is_a_404(client: TestClient) -> None:
    register(client)

    assert client.delete("/api/bookmarks/never-saved").status_code == 404
    assert client.post("/api/bookmarks", json=article()).status_code == 201
    assert client.delete("/api/bookmarks/article-1").status_code == 204
    assert client.get("/api/bookmarks").json()["bookmarks"] == []


# ---------------------------------------------------------------- migration


def test_bookmarks_predating_accounts_are_carried_across(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE bookmarks (
            id            TEXT PRIMARY KEY,
            payload       TEXT NOT NULL,
            bookmarked_at TEXT NOT NULL
        );
        """
    )
    legacy.execute(
        "INSERT INTO bookmarks (id, payload, bookmarked_at) VALUES (?, ?, ?)",
        ("article-old", json.dumps(article("article-old", "Saved before accounts")), WHEN.isoformat()),
    )
    legacy.commit()
    legacy.close()

    database = Database(path, stale_ttl_seconds=3600)

    columns = {row["name"] for row in database._conn.execute("PRAGMA table_info(bookmarks)")}
    assert "user_id" in columns

    # Kept, unowned: they predate any account, so no reader could claim them.
    orphaned = database.bookmarks_list("")
    assert [bookmark.id for bookmark in orphaned] == ["article-old"]
    database.close()
