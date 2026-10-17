"""Story threads: what a cluster leaves behind once its feed page is gone."""

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
from app.routers import threads
from app.schemas import Article, StoryThread

WHEN = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "newsapi_key": "newsapi-key",
        "newsdata_key": "newsdata-key",
        "freenewsapi_key": "freenewsapi-key",
        "database_path": str(tmp_path / "test.db"),
    }
    values.update(overrides)
    return Settings(**values)


def article(article_id: str, title: str, source: str) -> Article:
    return Article(
        id=article_id,
        title=title,
        description="A short summary.",
        url=f"https://example.com/{article_id}",
        source=source,
        published_at=WHEN,
        provider="newsdata",
    )


def thread() -> StoryThread:
    return StoryThread(
        lead_id="lead",
        count=3,
        sources=["The Verge", "Reuters", "CNET"],
        coverage=[
            article("lead", "Tesla unveils the flying car Roadster", "The Verge"),
            article("also", "Tesla's flying car Roadster stuns in demo", "Reuters"),
            article("third", "Dramatic flying car Roadster lands in Detroit", "CNET"),
        ],
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def database(settings: Settings) -> Database:
    db = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    yield db
    db.close()


@pytest.fixture
def client(settings: Settings, database: Database):
    """A minimal app carrying only the router under test.

    `app.main` is avoided for the same reason as in `test_auth`: its lifespan
    opens the developer's real database and starts the aggregator.
    """
    app = FastAPI()
    install_error_handlers(app)
    api = APIRouter(prefix="/api")
    api.include_router(threads.router)
    app.include_router(api)

    app.state.settings = settings
    app.state.database = database
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_database] = lambda: database

    with TestClient(app) as test_client:
        yield test_client


# ----------------------------------------------------------------- resolving


def test_a_story_only_one_outlet_ran_has_no_thread(client: TestClient) -> None:
    # Soft rather than a 404: the story page asks about every article it shows.
    response = client.get("/api/threads/nobody-ran-this")

    assert response.status_code == 200
    assert response.json() == {"thread": None}


def test_the_lead_article_resolves_to_its_thread(client: TestClient, database: Database) -> None:
    database.threads_remember([thread()])

    body = client.get("/api/threads/lead").json()["thread"]

    assert body["lead_id"] == "lead"
    assert body["count"] == 3
    assert body["sources"] == ["The Verge", "Reuters", "CNET"]
    assert [item["id"] for item in body["coverage"]] == ["lead", "also", "third"]


def test_a_copy_that_is_not_the_lead_still_finds_the_thread(
    client: TestClient, database: Database
) -> None:
    # A reader can arrive through a search hit or a bookmark on any copy.
    database.threads_remember([thread()])

    assert client.get("/api/threads/third").json()["thread"]["lead_id"] == "lead"


def test_an_unrelated_article_is_not_dragged_into_a_thread(
    client: TestClient, database: Database
) -> None:
    database.threads_remember([thread()])

    assert client.get("/api/threads/something-else").json()["thread"] is None


def test_the_other_outlets_headlines_travel_with_the_thread(
    client: TestClient, database: Database
) -> None:
    # This is the whole point of keeping a thread: the outlets' own wording,
    # side by side, so the reader can see how the story was framed.
    database.threads_remember([thread()])

    coverage = client.get("/api/threads/lead").json()["thread"]["coverage"]

    assert coverage[1]["title"] == "Tesla's flying car Roadster stuns in demo"
    assert coverage[1]["source"] == "Reuters"


# ------------------------------------------------------------------ storing


def test_a_thread_is_replaced_rather_than_appended_to(database: Database) -> None:
    # The clusterer recomputes from scratch each pass, so the store must too —
    # otherwise a story that stops being clustered would keep claiming outlets.
    database.threads_remember([thread()])
    database.threads_remember(
        [StoryThread(lead_id="lead", count=2, sources=["The Verge"], coverage=[])]
    )

    stored = database.thread_for("lead")

    assert stored is not None
    assert stored.count == 2
    assert stored.coverage == []


def test_coverage_is_capped_however_many_copies_arrive(database: Database) -> None:
    # A syndicated wire story can arrive in bulk; the thread must stay bounded.
    coverage = [article(f"copy-{index}", f"Same story, take {index}", "Wire") for index in range(40)]

    database.threads_remember(
        [StoryThread(lead_id="wired", count=40, sources=["Wire"], coverage=coverage)]
    )

    stored = database.thread_for("wired")
    assert stored is not None
    assert len(stored.coverage) == 24


def test_a_thread_whose_payload_no_longer_validates_is_dropped(database: Database) -> None:
    # One unreadable row must not raise on every story the reader opens.
    database._conn.execute(
        "INSERT INTO threads (lead_id, payload, seen_at) VALUES (?, ?, ?)",
        ("broken", json.dumps({"lead_id": "broken"}), WHEN.isoformat()),
    )
    database._conn.commit()

    assert database.thread_for("broken") is None
