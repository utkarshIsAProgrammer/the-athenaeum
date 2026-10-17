"""The guards in front of every route: request counting and header shape.

The middleware is pure ASGI, so these drive it through a real `TestClient`
rather than calling it directly — a wrong `send`/`receive` contract is exactly
the sort of thing that only shows up when something else is driving it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import install_error_handlers
from app.middleware import GuardsMiddleware, SecurityHeadersMiddleware, SlidingWindow


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_path": str(tmp_path / "unused.db"),
        "rate_limit_requests": 3,
        "rate_limit_window_seconds": 60,
        "rate_limit_auth_requests": 2,
        "rate_limit_auth_window_seconds": 300,
        "max_request_bytes": 1024,
    }
    values.update(overrides)
    return Settings(**values)


def build(settings: Settings) -> FastAPI:
    """Guards first, headers last, so the headers are the outermost layer.

    That is the order `main.py` uses, and it is the one that matters: a refusal
    the guards emit has to come back carrying the same headers as anything else.
    """
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/api/feed")
    async def feed() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/thing")
    async def thing() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/auth/login")
    async def login() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(GuardsMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, hsts=False)
    return app


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(build(make_settings(tmp_path))) as test_client:
        yield test_client


# ------------------------------------------------------------- sliding window


def test_a_window_counts_only_what_is_inside_it() -> None:
    window = SlidingWindow(2, 10.0)

    assert window.allow("a", now=0.0) is True
    assert window.allow("a", now=1.0) is True
    assert window.allow("a", now=2.0) is False
    # The first hit has fallen out of the ten-second window by now.
    assert window.allow("a", now=10.5) is True


def test_one_client_spending_its_allowance_does_not_spend_anothers() -> None:
    window = SlidingWindow(1, 10.0)

    assert window.allow("a", now=0.0) is True
    assert window.allow("b", now=0.0) is True
    assert window.allow("a", now=0.0) is False


def test_the_key_table_is_bounded() -> None:
    window = SlidingWindow(1, 10.0, max_keys=4)

    # Every one of these would be allowed on its own first hit, so the only way
    # memory stays flat is the sweep that runs when the table is full.
    for index in range(50):
        window.allow(f"client-{index}", now=float(index))

    assert len(window._hits) <= 4


# ------------------------------------------------------------------- counting


def test_a_client_is_refused_once_it_has_spent_its_requests(client: TestClient) -> None:
    for _ in range(3):
        assert client.get("/api/feed").status_code == 200

    refused = client.get("/api/feed")

    assert refused.status_code == 429
    assert "too many requests" in refused.json()["error"].lower()


def test_a_refusal_carries_the_same_headers_as_a_response(client: TestClient) -> None:
    for _ in range(4):
        refused = client.get("/api/feed")

    assert refused.status_code == 429
    assert refused.headers["x-content-type-options"] == "nosniff"
    assert refused.headers["x-frame-options"] == "DENY"


def test_sign_in_is_counted_separately_and_much_more_loosely(client: TestClient) -> None:
    # Two sign-in attempts, then the third is refused — while the ordinary feed
    # ceiling is still untouched.
    assert client.post("/api/auth/login").status_code == 200
    assert client.post("/api/auth/login").status_code == 200
    assert client.post("/api/auth/login").status_code == 429
    assert client.get("/api/feed").status_code == 200


def test_the_health_check_is_never_counted(tmp_path: Path) -> None:
    # Refusing the platform's own prober gets the instance recycled, so it has
    # to keep answering for ever.
    with TestClient(build(make_settings(tmp_path, rate_limit_requests=1))) as client:
        for _ in range(10):
            assert client.get("/api/health").status_code == 200


def test_a_preflight_is_never_counted(tmp_path: Path) -> None:
    with TestClient(build(make_settings(tmp_path, rate_limit_requests=1))) as client:
        client.get("/api/feed")
        for _ in range(10):
            assert client.options("/api/feed").status_code != 429


def test_the_forwarded_address_is_what_gets_counted(tmp_path: Path) -> None:
    with TestClient(build(make_settings(tmp_path, rate_limit_requests=1))) as client:
        assert client.get("/api/feed", headers={"x-forwarded-for": "1.1.1.1"}).status_code == 200
        assert client.get("/api/feed", headers={"x-forwarded-for": "2.2.2.2"}).status_code == 200
        # The first address is the one that has now spent its allowance.
        refused = client.get("/api/feed", headers={"x-forwarded-for": "1.1.1.1"})
        assert refused.status_code == 429


# ------------------------------------------------------------- proxy secret


def test_a_caller_that_did_not_come_from_the_proxy_is_turned_away(tmp_path: Path) -> None:
    # The API is on a public URL that anyone can resolve. Without this, a
    # stranger sends a fresh `x-forwarded-for` per request, counts as a new
    # client every time, and spends the day's provider allowance in a minute.
    with TestClient(build(make_settings(tmp_path, internal_api_secret="s3cret"))) as client:
        refused = client.get("/api/feed")

        assert refused.status_code == 403
        assert "only reachable through the site" in refused.json()["error"]


def test_the_proxys_request_is_let_through(tmp_path: Path) -> None:
    with TestClient(build(make_settings(tmp_path, internal_api_secret="s3cret"))) as client:
        allowed = client.get("/api/feed", headers={"x-internal-secret": "s3cret"})

        assert allowed.status_code == 200


def test_a_near_miss_is_not_good_enough(tmp_path: Path) -> None:
    with TestClient(build(make_settings(tmp_path, internal_api_secret="s3cret"))) as client:
        assert client.get("/api/feed", headers={"x-internal-secret": "s3cre"}).status_code == 403
        assert client.get("/api/feed", headers={"x-internal-secret": "s3cret "}).status_code == 403


def test_no_secret_configured_means_no_check(tmp_path: Path) -> None:
    # The default, and the right answer for an API only the proxy can reach.
    with TestClient(build(make_settings(tmp_path))) as client:
        assert client.get("/api/feed").status_code == 200


def test_the_platforms_own_prober_never_needs_the_secret(tmp_path: Path) -> None:
    # A health check the host cannot read is a health check that gets the
    # instance recycled, and the prober has no way to be told the secret.
    settings = make_settings(tmp_path, internal_api_secret="s3cret", rate_limit_requests=1)
    with TestClient(build(settings)) as client:
        for _ in range(5):
            assert client.get("/api/health").status_code == 200


def test_the_secret_is_checked_before_the_allowance_is_spent(tmp_path: Path) -> None:
    # A stranger with a forged address must not even reach the counter, or the
    # guard would be protecting the quota with a broken ruler.
    settings = make_settings(tmp_path, internal_api_secret="s3cret", rate_limit_requests=1)
    with TestClient(build(settings)) as client:
        assert client.get("/api/feed", headers={"x-forwarded-for": "9.9.9.9"}).status_code == 403
        # The refusal spent nothing, so the proxy still has its one request.
        assert client.get("/api/feed", headers={"x-internal-secret": "s3cret"}).status_code == 200


# ----------------------------------------------------------------- body size


def test_a_body_larger_than_the_ceiling_is_refused_before_it_is_read(
    client: TestClient,
) -> None:
    refused = client.post("/api/thing", content=b"x" * 4096)

    assert refused.status_code == 413
    assert "larger" in refused.json()["error"].lower()


def test_a_body_that_fits_is_let_through(client: TestClient) -> None:
    assert client.post("/api/thing", content=b"x" * 100).status_code == 200


def test_a_broken_content_length_is_refused(client: TestClient) -> None:
    refused = client.post(
        "/api/thing",
        content=b"{}",
        headers={"content-length": "not-a-number"},
    )

    assert refused.status_code == 400


# ------------------------------------------------------------ header shaping


def test_every_response_carries_the_fixed_headers(client: TestClient) -> None:
    headers = client.get("/api/feed").headers

    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert headers["content-security-policy"].startswith("default-src 'none'")
    assert "payment=()" in headers["permissions-policy"]


def test_hsts_is_withheld_until_the_deployment_is_on_tls(tmp_path: Path) -> None:
    plain = FastAPI()
    plain.add_middleware(SecurityHeadersMiddleware, hsts=False)
    with TestClient(plain) as client:
        assert "strict-transport-security" not in client.get("/").headers

    secure = FastAPI()
    secure.add_middleware(SecurityHeadersMiddleware, hsts=True)
    with TestClient(secure) as client:
        assert "max-age=" in client.get("/").headers["strict-transport-security"]


def test_a_header_a_route_already_set_is_not_overwritten(tmp_path: Path) -> None:
    app = FastAPI()

    @app.get("/")
    async def root():
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": True}, headers={"x-frame-options": "SAMEORIGIN"})

    app.add_middleware(SecurityHeadersMiddleware, hsts=False)
    with TestClient(app) as client:
        assert client.get("/").headers["x-frame-options"] == "SAMEORIGIN"
