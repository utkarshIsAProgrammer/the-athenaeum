"""Per-reader AI keys and the summaries they buy.

Nothing here reaches a real provider: `ai.ask` is patched in every test that
would otherwise post upstream, so the suite stays offline and spends nobody's
free tier.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.crypto import SecretsUnavailable, decrypt, encrypt
from app.db import Database
from app.dependencies import get_aggregator, get_database
from app.errors import install_error_handlers
from app.providers import ai
from app.routers import ai as ai_router
from app.routers import auth
from app.schemas import Article, ArticleDetail, StoryThread, Summary

WHEN = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
READER = {"email": "reader@example.com", "password": "correct horse battery"}

SECRET = "a-test-secret-key"
OTHER_SECRET = "a-different-test-secret"
KEY = "gsk_averysecretlookingkey0001234"

# Comfortably past `MIN_SOURCE_CHARS`, since a stub has nothing to condense.
BODY = "The council approved the measure on Thursday after a six-hour session. " * 8
SHORT = "Too short to summarise."


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "newsapi_key": "newsapi-key",
        "newsdata_key": "newsdata-key",
        "freenewsapi_key": "freenewsapi-key",
        "database_path": str(tmp_path / "test.db"),
        "secret_key": SECRET,
    }
    values.update(overrides)
    return Settings(**values)


def detail(article_id: str = "article-1", body: str | None = BODY) -> ArticleDetail:
    return ArticleDetail(
        id=article_id,
        title="Council approves the measure",
        description="A short standfirst." if body is None else "One line about it.",
        url=f"https://example.com/{article_id}",
        source="Example News",
        published_at=WHEN,
        provider="freenewsapi",
        body=body,
    )


class FakeNews:
    """Stands in for the aggregator: one article, and no real HTTP client."""

    def __init__(self, article: ArticleDetail | None = None) -> None:
        self.detail = article

    async def article(self, article_id: str) -> ArticleDetail | None:
        return self.detail

    def require_client(self) -> Any:
        # `ai.ask` is patched wherever it would be used, so nothing ever posts.
        return None


@pytest.fixture
def env(tmp_path: Path):
    """A minimal app carrying auth and ai, with a fake aggregator behind it."""
    settings = make_settings(tmp_path)
    database = Database(settings.database_file, settings.stale_cache_ttl_seconds)
    news = FakeNews(detail())

    app = FastAPI()
    install_error_handlers(app)
    api = APIRouter(prefix="/api")
    api.include_router(auth.router)
    api.include_router(ai_router.router)
    app.include_router(api)

    app.state.settings = settings
    app.state.database = database
    app.state.aggregator = news
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_aggregator] = lambda: news

    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, database=database, settings=settings, news=news
        )

    database.close()


def sign_in(env: SimpleNamespace) -> None:
    response = env.client.post("/api/auth/register", json=READER)
    assert response.status_code == 201


def store_key(env: SimpleNamespace, provider: str = ai.GROQ, key: str = KEY):
    return env.client.put(f"/api/ai/keys/{provider}", json={"key": key})


# ------------------------------------------------------------------- crypto


def test_encrypt_round_trips_without_leaving_the_key_readable() -> None:
    ciphertext = encrypt(SECRET, KEY)

    assert KEY not in ciphertext
    assert decrypt(SECRET, ciphertext) == KEY
    # Fernet seals a timestamp and an IV into every message, so the same key
    # stored twice does not produce the same row.
    assert encrypt(SECRET, KEY) != ciphertext


def test_encrypt_refuses_with_no_secret_configured() -> None:
    with pytest.raises(SecretsUnavailable):
        encrypt("", KEY)


def test_decrypt_refuses_a_rotated_secret() -> None:
    ciphertext = encrypt(SECRET, KEY)

    with pytest.raises(SecretsUnavailable):
        decrypt(OTHER_SECRET, ciphertext)


# --------------------------------------------------------------- parse_reply


def test_parse_reply_splits_the_paragraph_from_the_bullets() -> None:
    summary = ai.parse_reply(
        "The council approved the measure after a long session.\n"
        "- It passed by seven votes\n"
        "- Work starts in March\n"
        "- Opponents plan an appeal\n"
    )

    assert summary.text == "The council approved the measure after a long session."
    assert summary.bullets == [
        "It passed by seven votes",
        "Work starts in March",
        "Opponents plan an appeal",
    ]


def test_parse_reply_accepts_a_numbered_list() -> None:
    # Models drift into numbering often enough that it must not read as prose.
    summary = ai.parse_reply("What happened.\n1. First fact\n2) Second fact\n")

    assert summary.text == "What happened."
    assert summary.bullets == ["First fact", "Second fact"]


def test_parse_reply_promotes_a_bullet_when_there_is_no_paragraph() -> None:
    summary = ai.parse_reply("- Only a bullet came back\n- And another one\n")

    assert summary.text == "Only a bullet came back"
    assert summary.bullets == ["And another one"]


def test_parse_reply_keeps_prose_that_never_bulleted() -> None:
    summary = ai.parse_reply("The whole thing arrived as one paragraph.\n")

    assert summary.text == "The whole thing arrived as one paragraph."
    assert summary.bullets == []


def test_parse_reply_ignores_a_sentence_that_merely_starts_with_a_year() -> None:
    # "2024. The year..." is prose, not a list item, and must not be shredded.
    summary = ai.parse_reply("2024. The year the rules changed.\n")

    assert summary.text == "2024. The year the rules changed."


# -------------------------------------------------------------- build_prompt


def test_prompt_truncates_on_a_word_boundary() -> None:
    prompt = ai.build_prompt(detail(body="alpha beta gamma delta"), max_chars=12)

    assert "Article:\nalpha beta…" in prompt
    assert "gamm" not in prompt, "a word must not be sliced in half"


def test_prompt_carries_the_byline_and_the_instructions() -> None:
    prompt = ai.build_prompt(detail(), max_chars=6000)

    assert "Headline: Council approves the measure" in prompt
    assert "Outlet: Example News" in prompt
    assert "16 October 2026" in prompt
    assert "three key facts" in prompt


def headline(article_id: str, title: str, source: str) -> Article:
    return Article(
        id=article_id,
        title=title,
        url=f"https://example.com/{article_id}",
        source=source,
        published_at=WHEN,
        provider="newsdata",
    )


def test_prompt_offers_the_other_outlets_that_ran_the_story() -> None:
    # Three providers never agree on wording, so a story five newsrooms led on
    # is what tells the model it is not a local curiosity.
    prompt = ai.build_prompt(
        detail(),
        max_chars=6000,
        coverage=[
            headline("other-1", "Council approves the measure in late vote", "Reuters"),
            headline("other-2", "Measure passes after six-hour session", "The Verge"),
        ],
    )

    assert "Other outlets reporting this, headline only:" in prompt
    assert "- Reuters: Council approves the measure in late vote" in prompt
    assert "- The Verge: Measure passes after six-hour session" in prompt


def test_the_story_being_summarised_is_not_listed_as_its_own_coverage() -> None:
    prompt = ai.build_prompt(
        detail(article_id="article-1"),
        max_chars=6000,
        coverage=[headline("article-1", "The same story again", "Example News")],
    )

    assert "Other outlets" not in prompt


def test_one_outlet_is_named_once_however_many_copies_it_syndicated() -> None:
    prompt = ai.build_prompt(
        detail(),
        max_chars=6000,
        coverage=[
            headline("a", "First wording of the story here", "Reuters"),
            headline("b", "Second wording of the same story", "Reuters"),
            headline("c", "A third copy of the same thing", ""),
        ],
    )

    assert prompt.count("- Reuters:") == 1
    assert "Second wording" not in prompt
    assert "A third copy" not in prompt, "an unnamed outlet is not an attribution"


def test_the_coverage_list_is_capped() -> None:
    prompt = ai.build_prompt(
        detail(),
        max_chars=6000,
        coverage=[headline(f"c{n}", f"Wording number {n} of the story", f"Outlet {n}") for n in range(20)],
    )

    assert prompt.count("- Outlet ") == ai.MAX_COVERAGE


def test_no_coverage_leaves_the_prompt_as_it_was() -> None:
    without = ai.build_prompt(detail(), max_chars=6000)
    empty = ai.build_prompt(detail(), max_chars=6000, coverage=[])

    assert without == empty
    assert "Other outlets" not in without


def test_the_summarise_route_hands_the_threads_coverage_to_the_model(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cluster that found the other copies died with its feed page; the
    # thread is what survives, so it is what has to reach the prompt.
    env.database.threads_remember(
        [
            StoryThread(
                lead_id="article-1",
                count=2,
                sources=["Example News", "Reuters"],
                coverage=[
                    headline("other", "Council approves the measure in late vote", "Reuters")
                ],
            )
        ]
    )

    calls: list[dict[str, Any]] = []

    async def fake_ask(client, **kwargs):
        calls.append(kwargs)
        return Summary(text="It passed.")

    monkeypatch.setattr(ai, "ask", fake_ask)
    sign_in(env)
    store_key(env)

    assert env.client.post("/api/ai/summarise", json={"article_id": "article-1"}).status_code == 200
    assert "- Reuters: Council approves the measure in late vote" in calls[0]["prompt"]


# ------------------------------------------------------------------ api keys


def test_key_routes_require_an_account(env: SimpleNamespace) -> None:
    client = env.client

    assert client.get("/api/ai/keys").status_code == 401
    assert client.put(f"/api/ai/keys/{ai.GROQ}", json={"key": KEY}).status_code == 401
    assert client.delete(f"/api/ai/keys/{ai.GROQ}").status_code == 401


def test_saving_a_key_reports_it_without_ever_returning_it(env: SimpleNamespace) -> None:
    sign_in(env)
    response = store_key(env)

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["label"] == "Groq"
    assert body["hint"].endswith(KEY[-4:])
    assert KEY not in response.text

    listed = env.client.get("/api/ai/keys").json()["keys"]
    assert KEY not in env.client.get("/api/ai/keys").text
    assert [state["provider"] for state in listed] == list(ai.PROVIDERS)
    assert [state["configured"] for state in listed] == [True, False, False]
    assert listed[0]["hint"].endswith(KEY[-4:])
    assert listed[1]["hint"] is None


def test_a_key_is_stored_encrypted(env: SimpleNamespace) -> None:
    sign_in(env)
    store_key(env)

    rows = env.database._conn.execute("SELECT ciphertext FROM provider_keys").fetchall()
    assert len(rows) == 1
    assert KEY not in rows[0]["ciphertext"]
    assert decrypt(SECRET, rows[0]["ciphertext"]) == KEY


def test_saving_a_key_is_refused_until_a_secret_is_configured(env: SimpleNamespace) -> None:
    sign_in(env)
    env.settings.secret_key = ""

    response = store_key(env)
    assert response.status_code == 503
    assert "SECRET_KEY" in response.json()["error"]
    # And nothing was written in the clear as a fallback.
    rows = env.database._conn.execute("SELECT COUNT(*) AS n FROM provider_keys").fetchone()
    assert rows["n"] == 0


def test_replacing_a_key_keeps_one_row(env: SimpleNamespace) -> None:
    sign_in(env)
    store_key(env, key=KEY)
    second = store_key(env, key="gsk_a_completely_different_key5678")

    assert second.json()["hint"].endswith("5678")
    count = env.database._conn.execute("SELECT COUNT(*) AS n FROM provider_keys").fetchone()
    assert count["n"] == 1


def test_an_unknown_provider_is_a_404(env: SimpleNamespace) -> None:
    sign_in(env)

    assert store_key(env, provider="mistral").status_code == 404
    assert env.client.delete("/api/ai/keys/mistral").status_code == 404


def test_deleting_a_key_that_was_never_stored_is_a_404(env: SimpleNamespace) -> None:
    sign_in(env)

    assert env.client.delete(f"/api/ai/keys/{ai.GROQ}").status_code == 404

    store_key(env)
    assert env.client.delete(f"/api/ai/keys/{ai.GROQ}").status_code == 204
    listed = env.client.get("/api/ai/keys").json()["keys"]
    assert listed[0]["configured"] is False


def test_keys_are_scoped_per_reader(env: SimpleNamespace) -> None:
    sign_in(env)
    store_key(env)
    env.client.post("/api/auth/logout")

    env.client.post(
        "/api/auth/register",
        json={"email": "other@example.com", "password": "a different passphrase"},
    )
    assert env.client.get("/api/ai/keys").json()["keys"][0]["configured"] is False


# ----------------------------------------------------------------- summarise


def test_summarise_requires_an_account(env: SimpleNamespace) -> None:
    response = env.client.post("/api/ai/summarise", json={"article_id": "article-1"})

    assert response.status_code == 401
    assert response.json()["error"] == "Sign in to continue."


def test_summarise_asks_for_a_key_when_none_is_stored(env: SimpleNamespace) -> None:
    sign_in(env)

    response = env.client.post("/api/ai/summarise", json={"article_id": "article-1"})

    assert response.status_code == 409
    assert "Add a free AI key" in response.json()["error"]


def test_summarise_reports_a_missing_article_before_anything_else(
    env: SimpleNamespace,
) -> None:
    sign_in(env)
    env.news.detail = None

    # No key stored and no article: the article is the more useful complaint.
    response = env.client.post("/api/ai/summarise", json={"article_id": "gone"})
    assert response.status_code == 404


def test_summarise_refuses_a_story_with_nothing_to_condense(env: SimpleNamespace) -> None:
    sign_in(env)
    store_key(env)
    env.news.detail = detail(body=None)  # only the short standfirst is left

    response = env.client.post("/api/ai/summarise", json={"article_id": "article-1"})

    assert response.status_code == 422
    assert "not enough text" in response.json()["error"]


def test_summarise_returns_and_then_replays_a_cached_summary(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_ask(client, **kwargs):
        calls.append(kwargs)
        return Summary(text="The measure passed.", bullets=["Seven votes", "Work starts in March"])

    monkeypatch.setattr(ai, "ask", fake_ask)

    sign_in(env)
    store_key(env)

    first = env.client.post("/api/ai/summarise", json={"article_id": "article-1"})
    assert first.status_code == 200
    body = first.json()
    assert body["provider"] == "groq"
    assert body["label"] == "Groq"
    assert body["cached"] is False
    assert body["summary"]["text"] == "The measure passed."
    assert body["summary"]["bullets"] == ["Seven votes", "Work starts in March"]

    # The decrypted key reached the provider, and the prompt carried the body.
    assert calls[0]["api_key"] == KEY
    assert BODY.strip() in calls[0]["prompt"]
    assert calls[0]["timeout"] == env.settings.ai_request_timeout_seconds

    second = env.client.post("/api/ai/summarise", json={"article_id": "article-1"})
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert len(calls) == 1, "a cached summary must not spend the reader's quota again"


def test_refresh_ignores_the_cache(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def fake_ask(client, **kwargs):
        nonlocal calls
        calls += 1
        return Summary(text=f"Attempt {calls}.")

    monkeypatch.setattr(ai, "ask", fake_ask)

    sign_in(env)
    store_key(env)

    env.client.post("/api/ai/summarise", json={"article_id": "article-1"})
    refreshed = env.client.post(
        "/api/ai/summarise", json={"article_id": "article-1", "refresh": True}
    )

    assert refreshed.json()["cached"] is False
    assert refreshed.json()["summary"]["text"] == "Attempt 2."
    assert calls == 2


def test_summarise_without_a_provider_prefers_the_stored_order(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    used: list[str] = []

    async def fake_ask(client, **kwargs):
        used.append(kwargs["provider"])
        return Summary(text="Something happened.")

    monkeypatch.setattr(ai, "ask", fake_ask)

    sign_in(env)
    store_key(env, provider=ai.GEMINI)

    response = env.client.post("/api/ai/summarise", json={"article_id": "article-1"})

    # Only Gemini is stored, so it is chosen even though Groq leads the order.
    assert response.json()["provider"] == "gemini"
    assert used == ["gemini"]


def test_summarise_refuses_a_provider_the_reader_has_no_key_for(
    env: SimpleNamespace,
) -> None:
    sign_in(env)
    store_key(env, provider=ai.GEMINI)

    response = env.client.post(
        "/api/ai/summarise", json={"article_id": "article-1", "provider": ai.GROQ}
    )

    assert response.status_code == 409
    assert "Groq" in response.json()["error"]


def test_summarise_refuses_an_unknown_provider(env: SimpleNamespace) -> None:
    sign_in(env)
    store_key(env)

    response = env.client.post(
        "/api/ai/summarise", json={"article_id": "article-1", "provider": "mistral"}
    )
    assert response.status_code == 404


def test_a_rejected_key_surfaces_as_the_readers_own_problem(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_ask(client, **kwargs):
        raise ai.AiError(
            "Groq rejected that key. Check it and save it again.",
            provider="groq",
            status_code=400,
        )

    monkeypatch.setattr(ai, "ask", fake_ask)

    sign_in(env)
    store_key(env)

    response = env.client.post("/api/ai/summarise", json={"article_id": "article-1"})
    assert response.status_code == 400
    assert "rejected that key" in response.json()["error"]
    # A failure must not be cached as though it were an answer.
    assert env.database.summary_get("article-1", ai.GROQ) is None


def test_a_rate_limit_keeps_its_own_status(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_ask(client, **kwargs):
        raise ai.AiError("Groq is rate limiting this key.", provider="groq", status_code=429)

    monkeypatch.setattr(ai, "ask", fake_ask)

    sign_in(env)
    store_key(env)

    response = env.client.post("/api/ai/summarise", json={"article_id": "article-1"})
    assert response.status_code == 429


def test_an_unreachable_provider_is_a_502(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_ask(client, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(ai, "ask", fake_ask)

    sign_in(env)
    store_key(env)

    response = env.client.post("/api/ai/summarise", json={"article_id": "article-1"})
    assert response.status_code == 502
    assert response.json()["error"] == "Groq could not be reached."


def test_a_rotated_secret_makes_the_summarise_route_say_so(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_ask(client, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the request should never leave")

    monkeypatch.setattr(ai, "ask", fake_ask)

    sign_in(env)
    store_key(env)
    env.settings.secret_key = OTHER_SECRET

    response = env.client.post("/api/ai/summarise", json={"article_id": "article-1"})
    assert response.status_code == 503


def test_a_retired_model_is_explained_rather_than_reported_as_a_bare_404() -> None:
    # The old wording was "OpenRouter could not answer (HTTP 404).", which named
    # neither the cause nor the model — and a retired free model is the usual
    # reason OpenRouter answers 404.
    error = ai._failure(ai.OPENROUTER, 404, "meta-llama/llama-3.3-70b-instruct:free")

    assert error.status_code == 502
    assert "meta-llama/llama-3.3-70b-instruct:free" in str(error)
    assert "OpenRouter" in str(error)
    assert "HTTP 404" not in str(error)


def test_a_404_from_a_provider_that_hides_the_model_still_reads_sensibly() -> None:
    error = ai._failure(ai.GEMINI, 404)

    assert error.status_code == 502
    assert "the model this deployment is set to" in str(error)


async def test_a_reasoning_model_that_answers_with_nothing_is_not_a_500() -> None:
    # A model advertising `reasoning.default_enabled: true` — as
    # `nvidia/nemotron-3-super-120b-a12b:free` does — spends the whole 700-token
    # budget thinking and returns `content: null`. That reached `parse_reply` as
    # `None`, raised `AttributeError`, and surfaced as "Internal server error."
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": None}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ai.AiError) as raised:
            await ai._chat(
                client,
                provider=ai.OPENROUTER,
                url="https://openrouter.ai/api/v1/chat/completions",
                model="nvidia/nemotron-3-super-120b-a12b:free",
                api_key=KEY,
                prompt="Summarise this story.",
            )

    # It is the deployment's model setting at fault, so the message has to say
    # what happened rather than blame the reader's key.
    assert "OpenRouter" in str(raised.value)
    assert "empty" in str(raised.value)
    assert raised.value.status_code != 500


def pool_busy() -> httpx.Response:
    """What OpenRouter really answers when a `:free` model's pool is saturated."""
    return httpx.Response(
        429,
        json={
            "error": {
                "message": "Provider returned error",
                "code": 429,
                "metadata": {
                    "raw": "google/gemma-4-31b-it:free is temporarily rate-limited "
                    "upstream. Please retry shortly.",
                    "provider_name": "Google AI Studio",
                    "limit_source": "upstream_provider_shared_pool",
                },
            }
        },
    )


def answered(text: str = "The council approved the measure.") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": text}}]}
    )


async def test_a_busy_free_pool_is_retried_rather_than_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A shared pool saturates constantly, and it clears within seconds. Handing
    # the reader an error over it would make the free models useless.
    monkeypatch.setattr(ai, "_POOL_RETRY_SECONDS", 0)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return pool_busy() if len(seen) == 1 else answered()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await ai._chat(
            client,
            provider=ai.OPENROUTER,
            url="https://openrouter.ai/api/v1/chat/completions",
            model="google/gemma-4-31b-it:free",
            api_key=KEY,
            prompt="Summarise this story.",
        )

    assert reply == "The council approved the measure."
    assert len(seen) == 2


async def test_a_pool_that_stays_busy_does_not_blame_the_readers_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai, "_POOL_RETRY_SECONDS", 0)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return pool_busy()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ai.AiError) as raised:
            await ai._chat(
                client,
                provider=ai.OPENROUTER,
                url="https://openrouter.ai/api/v1/chat/completions",
                model="google/gemma-4-31b-it:free",
                api_key=KEY,
                prompt="Summarise this story.",
            )

    # One retry, then stop: the key is fine and telling the reader to change it
    # would send them chasing a problem they do not have.
    assert len(seen) == 2
    assert "free pool" in str(raised.value)
    assert "this key" not in str(raised.value)
    assert raised.value.status_code == 429


async def test_the_keys_own_rate_limit_is_not_retried() -> None:
    # Nothing about waiting fixes a key that has spent its quota, and a second
    # request would only dig the hole deeper.
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ai.AiError) as raised:
            await ai._chat(
                client,
                provider=ai.OPENROUTER,
                url="https://openrouter.ai/api/v1/chat/completions",
                model="google/gemma-4-31b-it:free",
                api_key=KEY,
                prompt="Summarise this story.",
            )

    assert len(seen) == 1
    assert "rate limiting this key" in str(raised.value)


async def test_openrouters_fallback_chain_rides_in_the_request_body() -> None:
    # OpenRouter routes across `models` itself, so a saturated pool costs a
    # couple of seconds instead of the reader's summary.
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return answered()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ai._chat(
            client,
            provider=ai.OPENROUTER,
            url="https://openrouter.ai/api/v1/chat/completions",
            model="google/gemma-4-31b-it:free",
            api_key=KEY,
            prompt="Summarise this story.",
            alternates=[
                "google/gemma-4-26b-a4b-it:free",
                "nvidia/nemotron-3.5-lightning:free",
            ],
        )

    assert sent[0]["models"] == [
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3.5-lightning:free",
    ]
    # It rejects a body carrying both keys, so `models` has to replace `model`.
    assert "model" not in sent[0]


async def test_the_fallback_chain_is_capped_at_three() -> None:
    # OpenRouter answers 400 "'models' array must have 3 items or fewer", so the
    # cap is not decorative.
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return answered()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ai._chat(
            client,
            provider=ai.OPENROUTER,
            url="https://openrouter.ai/api/v1/chat/completions",
            model="primary:free",
            api_key=KEY,
            prompt="Summarise this story.",
            alternates=["second:free", "third:free", "fourth:free"],
        )

    assert sent[0]["models"] == ["primary:free", "second:free", "third:free"]


async def test_openrouters_reasoning_pass_is_switched_off() -> None:
    # Reasoning is what produced `content: null` in the first place, and several
    # free models do it by default without saying so on their model card —
    # nvidia/nemotron-3.5-lightning:free spends all 700 tokens thinking and
    # answers with nothing. Asking for it off is cheaper and works either way.
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return answered()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ai._chat(
            client,
            provider=ai.OPENROUTER,
            url="https://openrouter.ai/api/v1/chat/completions",
            model="nvidia/nemotron-3.5-lightning:free",
            api_key=KEY,
            prompt="Summarise this story.",
        )

    assert sent[0]["reasoning"] == {"enabled": False}


async def test_groq_is_spared_the_openrouter_only_parameters() -> None:
    # Groq speaks the same dialect but rejects what it does not know, and it has
    # no router to hand a chain of models to.
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return answered()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ai._chat(
            client,
            provider=ai.GROQ,
            url="https://api.groq.com/openai/v1/chat/completions",
            model="llama-3.3-70b-versatile",
            api_key=KEY,
            prompt="Summarise this story.",
        )

    assert sent[0]["model"] == "llama-3.3-70b-versatile"
    assert "reasoning" not in sent[0]
    assert "models" not in sent[0]


def test_the_summarise_route_offers_openrouter_a_fallback_chain(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_ask(client, **kwargs):
        calls.append(kwargs)
        return Summary(text="It passed.")

    monkeypatch.setattr(ai, "ask", fake_ask)
    sign_in(env)
    store_key(env, provider=ai.OPENROUTER, key="sk-or-v1-averysecretlookingkey")

    response = env.client.post(
        "/api/ai/summarise", json={"article_id": "article-1", "provider": ai.OPENROUTER}
    )

    assert response.status_code == 200
    assert calls[0]["model"] == env.settings.ai_openrouter_model
    assert calls[0]["alternates"] == env.settings.ai_openrouter_fallback_list
    assert env.settings.ai_openrouter_model not in calls[0]["alternates"]


def test_a_provider_with_one_model_is_not_given_alternates(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Groq carries exactly the model it is asked for; a list would be noise.
    calls: list[dict[str, Any]] = []

    async def fake_ask(client, **kwargs):
        calls.append(kwargs)
        return Summary(text="It passed.")

    monkeypatch.setattr(ai, "ask", fake_ask)
    sign_in(env)
    store_key(env)

    assert env.client.post("/api/ai/summarise", json={"article_id": "article-1"}).status_code == 200
    assert calls[0]["alternates"] is None


def test_every_configured_model_is_named_rather_than_left_blank() -> None:
    # A blank or mistyped model is indistinguishable from a retired one at the
    # provider, and a 404 is the only clue either way.
    settings = Settings(
        newsapi_key="k",
        newsdata_key="k",
        freenewsapi_key="k",
        secret_key=SECRET,
    )

    assert settings.ai_groq_model
    assert settings.ai_openrouter_model
    assert settings.ai_gemini_model
    # The slug OpenRouter retired. Pinning the replacement is how the next
    # retirement gets noticed, rather than discovered from a reader's 404.
    assert settings.ai_openrouter_model != "meta-llama/llama-3.3-70b-instruct:free"
    # The reasoning model that answered with `content: null`. Its failure was a
    # 500 on the summarise route, so it is worth keeping out of the default.
    assert settings.ai_openrouter_model != "nvidia/nemotron-3-super-120b-a12b:free"


# ------------------------------------------------------------------- storage


def test_summaries_are_shared_between_providers_but_not_confused(
    env: SimpleNamespace,
) -> None:
    sign_in(env)
    env.database.summary_set("article-1", ai.GROQ, Summary(text="From Groq."), ttl_seconds=900)

    assert env.database.summary_get("article-1", ai.GROQ).text == "From Groq."
    assert env.database.summary_get("article-1", ai.GEMINI) is None


def test_an_expired_summary_is_swept(env: SimpleNamespace) -> None:
    sign_in(env)
    env.database.summary_set("article-1", ai.GROQ, Summary(text="Old."), ttl_seconds=-1)

    assert env.database.summary_get("article-1", ai.GROQ) is None
    rows = env.database._conn.execute("SELECT COUNT(*) AS n FROM summaries").fetchone()
    assert rows["n"] == 0
