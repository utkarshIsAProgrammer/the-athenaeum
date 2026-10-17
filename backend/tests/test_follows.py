"""Followed subjects and outlets: who can write them, and what survives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import Database
from app.dependencies import get_database
from app.errors import install_error_handlers
from app.routers import auth, follows

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


@pytest.fixture
def client(tmp_path: Path):
    """A minimal app carrying only the routers under test."""
    settings = make_settings(tmp_path)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)

    app = FastAPI()
    install_error_handlers(app)
    api = APIRouter(prefix="/api")
    api.include_router(auth.router)
    api.include_router(follows.router)
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


def values(payload: dict[str, Any]) -> list[str]:
    return [follow["value"] for follow in payload["follows"]]


# --------------------------------------------------------------- who may read


def test_the_list_is_empty_rather_than_an_error_when_signed_out(client: TestClient) -> None:
    # The story page asks on every load so it can show what is already followed.
    response = client.get("/api/follows")

    assert response.status_code == 200
    assert response.json() == {"follows": []}


def test_writing_needs_an_account(client: TestClient) -> None:
    created = client.post("/api/follows", json={"kind": "topic", "value": "science"})
    removed = client.delete("/api/follows", params={"kind": "topic", "value": "science"})

    assert created.status_code == 401
    assert removed.status_code == 401
    assert created.json()["error"] == "Sign in to continue."


# ------------------------------------------------------------------ following


def test_a_followed_topic_comes_back_with_its_human_name(client: TestClient) -> None:
    register(client)

    created = client.post("/api/follows", json={"kind": "topic", "value": "technology"})

    assert created.status_code == 201
    # Resolved from the category list rather than stored, so the slug is the
    # only thing that can go stale.
    assert created.json() == {"kind": "topic", "value": "technology", "label": "Technology"}
    assert values(client.get("/api/follows").json()) == ["technology"]


def test_a_followed_outlet_is_its_own_label(client: TestClient) -> None:
    register(client)

    created = client.post("/api/follows", json={"kind": "source", "value": "The Verge"})

    assert created.status_code == 201
    assert created.json()["label"] == "The Verge"


def test_following_the_same_thing_twice_changes_nothing(client: TestClient) -> None:
    register(client)
    client.post("/api/follows", json={"kind": "source", "value": "Reuters"})

    again = client.post("/api/follows", json={"kind": "source", "value": "Reuters"})

    assert again.status_code == 201
    assert values(client.get("/api/follows").json()) == ["Reuters"]


def test_the_same_name_can_be_followed_as_a_topic_and_as_an_outlet(client: TestClient) -> None:
    # "science" is both a section and, conceivably, part of an outlet's name.
    register(client)
    client.post("/api/follows", json={"kind": "topic", "value": "science"})
    client.post("/api/follows", json={"kind": "source", "value": "science"})

    body = client.get("/api/follows").json()

    assert body["follows"] == [
        {"kind": "source", "value": "science", "label": "science"},
        {"kind": "topic", "value": "science", "label": "Science"},
    ]


def test_surrounding_space_is_trimmed_off_a_follow(client: TestClient) -> None:
    register(client)

    created = client.post("/api/follows", json={"kind": "source", "value": "  Le Monde  "})

    assert created.json()["value"] == "Le Monde"
    assert values(client.get("/api/follows").json()) == ["Le Monde"]


def test_unfollowing_removes_it(client: TestClient) -> None:
    register(client)
    client.post("/api/follows", json={"kind": "topic", "value": "health"})

    removed = client.delete("/api/follows", params={"kind": "topic", "value": "health"})

    assert removed.status_code == 204
    assert client.get("/api/follows").json()["follows"] == []


def test_unfollowing_something_never_followed_is_not_an_error(client: TestClient) -> None:
    # The control is a toggle, so a double tap must not produce a failure.
    register(client)

    assert client.delete("/api/follows", params={"kind": "topic", "value": "sports"}).status_code == 204


def test_follows_are_scoped_per_reader(client: TestClient) -> None:
    register(client, READER)
    client.post("/api/follows", json={"kind": "topic", "value": "science"})

    client.post("/api/auth/logout")
    register(client, OTHER)
    assert client.get("/api/follows").json()["follows"] == []

    client.post("/api/follows", json={"kind": "source", "value": "Reuters"})
    assert values(client.get("/api/follows").json()) == ["Reuters"]

    client.post("/api/auth/logout")
    client.post("/api/auth/login", json=READER)
    assert values(client.get("/api/follows").json()) == ["science"]


# ------------------------------------------------------------------ refusals


def test_a_topic_the_feed_cannot_answer_is_refused(client: TestClient) -> None:
    # Following a subject nothing can serve would leave a permanently empty
    # section on the personal page, so it is rejected at the door.
    register(client)

    response = client.post("/api/follows", json={"kind": "topic", "value": "gardening"})

    assert response.status_code == 422
    assert "Unknown topic" in response.json()["error"]


def test_the_front_page_placement_is_not_a_subject(client: TestClient) -> None:
    register(client)

    assert client.post("/api/follows", json={"kind": "topic", "value": "top"}).status_code == 422


def test_a_blank_follow_is_refused(client: TestClient) -> None:
    register(client)

    assert client.post("/api/follows", json={"kind": "source", "value": "   "}).status_code == 422


def test_a_kind_that_is_neither_topic_nor_source_is_refused(client: TestClient) -> None:
    register(client)

    assert client.post("/api/follows", json={"kind": "author", "value": "someone"}).status_code == 422
    assert client.delete("/api/follows", params={"kind": "author", "value": "x"}).status_code == 422
