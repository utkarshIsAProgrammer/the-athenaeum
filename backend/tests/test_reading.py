"""Reading history: the resume rail, the read/dim marks and their scoping."""

from __future__ import annotations

import json
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
from app.routers import auth, reading

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

    `app.main` is avoided for the same reason as in `test_auth`: its lifespan
    opens the developer's real database and starts the aggregator.
    """
    settings = make_settings(tmp_path)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)

    app = FastAPI()
    install_error_handlers(app)
    api = APIRouter(prefix="/api")
    api.include_router(auth.router)
    api.include_router(reading.router)
    app.include_router(api)

    app.state.settings = settings
    app.state.database = database
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_database] = lambda: database

    with TestClient(app) as test_client:
        yield test_client

    database.close()


def register(client: TestClient, credentials: dict[str, str] = READER):
    return client.post("/api/auth/register", json=credentials)


def ids(payload: dict[str, Any]) -> list[str]:
    return [entry["article_id"] for entry in payload["entries"]]


# ------------------------------------------------------------------ reading


def test_the_rail_is_empty_rather_than_an_error_when_signed_out(client: TestClient) -> None:
    # The feed asks on every page load, so a 401 here would be noise.
    response = client.get("/api/reading")

    assert response.status_code == 200
    assert response.json() == {"entries": [], "read_ids": []}


def test_writing_reading_history_needs_an_account(client: TestClient) -> None:
    assert client.post("/api/reading", json=article()).status_code == 401
    assert client.patch("/api/reading/article-1", json={"read": True}).status_code == 401

    assert client.post("/api/reading", json=article()).json()["error"] == "Sign in to continue."


def test_opening_stories_lists_them_newest_first(client: TestClient) -> None:
    register(client)

    assert client.post("/api/reading", json=article("a")).status_code == 201
    assert client.post("/api/reading", json=article("b")).status_code == 201

    body = client.get("/api/reading").json()
    assert ids(body) == ["b", "a"]


def test_the_article_travels_with_the_entry(client: TestClient) -> None:
    register(client)
    client.post("/api/reading", json=article("a", "The headline as it was read"))

    entry = client.get("/api/reading").json()["entries"][0]

    assert entry["article"]["title"] == "The headline as it was read"
    assert entry["article"]["url"] == "https://example.com/a"
    assert entry["read"] is False


def test_reopening_a_story_moves_it_up_without_duplicating_it(client: TestClient) -> None:
    register(client)
    client.post("/api/reading", json=article("a"))
    client.post("/api/reading", json=article("b"))
    client.post("/api/reading", json=article("a"))

    assert ids(client.get("/api/reading").json()) == ["a", "b"]


def test_reading_history_is_scoped_per_reader(client: TestClient) -> None:
    register(client, READER)
    client.post("/api/reading", json=article("mine"))

    client.post("/api/auth/logout")
    register(client, OTHER)
    assert client.get("/api/reading").json()["entries"] == []

    client.post("/api/reading", json=article("theirs"))
    assert ids(client.get("/api/reading").json()) == ["theirs"]

    client.post("/api/auth/logout")
    client.post("/api/auth/login", json=READER)
    assert ids(client.get("/api/reading").json()) == ["mine"]


# -------------------------------------------------------------------- marks


def test_marking_a_story_read_dim_lists_it(client: TestClient) -> None:
    register(client)
    client.post("/api/reading", json=article("a"))

    response = client.patch("/api/reading/a", json={"read": True})

    assert response.status_code == 200
    assert response.json()["read"] is True
    body = client.get("/api/reading").json()
    assert body["entries"][0]["read"] is True
    assert body["read_ids"] == ["a"]


def test_marking_a_story_unread_takes_it_off_the_dim_list(client: TestClient) -> None:
    register(client)
    client.post("/api/reading", json=article("a"))
    client.patch("/api/reading/a", json={"read": True})

    client.patch("/api/reading/a", json={"read": False})

    body = client.get("/api/reading").json()
    assert body["entries"][0]["read"] is False
    assert body["read_ids"] == []


def test_reopening_a_finished_story_does_not_make_it_unread_again(client: TestClient) -> None:
    # Reopening says "I was here", not "I have not read this".
    register(client)
    client.post("/api/reading", json=article("a"))
    client.patch("/api/reading/a", json={"read": True})

    client.post("/api/reading", json=article("a"))

    body = client.get("/api/reading").json()
    assert body["entries"][0]["read"] is True
    assert body["read_ids"] == ["a"]


def test_marking_read_a_story_never_opened_is_a_404(client: TestClient) -> None:
    register(client)

    response = client.patch("/api/reading/never-opened", json={"read": True})

    assert response.status_code == 404
    assert "history" in response.json()["error"]


def test_a_loose_value_cannot_flip_a_mark(client: TestClient) -> None:
    # Pydantic's lax mode would read "yes" and 1 as True, so a client could send
    # almost anything and still move a reader's mark. The app sends a real
    # boolean via JSON.stringify, so the contract is strict.
    register(client)
    client.post("/api/reading", json=article("a"))

    assert client.patch("/api/reading/a", json={"read": "yes"}).status_code == 422
    assert client.patch("/api/reading/a", json={"read": 1}).status_code == 422
    assert client.get("/api/reading").json()["read_ids"] == []


def test_a_payload_that_no_longer_validates_still_yields_a_tile(tmp_path: Path) -> None:
    # The id is enough to open the story, so one bad row must not sink the rail.
    settings = make_settings(tmp_path)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    user = database.user_create(READER["email"], "hash")

    database._conn.execute(
        "INSERT INTO reading (user_id, article_id, payload, opened_at, read) VALUES (?, ?, ?, ?, 0)",
        (user.id, "a", json.dumps({"title": "no id, no url"}), WHEN.isoformat()),
    )
    database._conn.commit()

    entries = database.reading_list(user.id)

    assert [entry.article_id for entry in entries] == ["a"]
    assert entries[0].article is None
    database.close()
