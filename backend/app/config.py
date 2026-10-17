from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration, read from `backend/.env` (see `.env.example`)."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # `production` asserts the deployment is public: it is served over TLS,
    # sits behind a proxy, and has a real secret. Setting it turns the
    # inconsistencies that are harmless in development into a startup error,
    # so the flip cannot be half-done — see `_check_production` below.
    environment: Literal["development", "production"] = "development"

    newsapi_key: str = ""
    newsdata_key: str = ""
    freenewsapi_key: str = ""

    host: str = "127.0.0.1"
    port: int = 8000

    database_path: str = "data/news.db"

    # A cached provider response is served directly for this long.
    cache_ttl_seconds: int = 1800
    # Once a provider's daily quota is gone we keep serving the last good
    # response until it is this old, so the feed degrades instead of breaking.
    stale_cache_ttl_seconds: int = 60 * 60 * 24 * 3

    # Free-tier ceilings. All providers are counted per calendar day (UTC).
    newsapi_daily_limit: int = 100
    newsdata_daily_limit: int = 200
    freenewsapi_daily_limit: int = 5000

    # FreeNewsApi caps a listing at ten items and allows two requests per
    # second; every listed article then needs one detail call for its image,
    # summary and outbound link. This is how many of those detail calls one
    # feed request may spend.
    freenewsapi_page_size: int = 10
    freenewsapi_detail_budget: int = 10
    freenewsapi_requests_per_second: float = 2.0

    request_timeout_seconds: float = 15.0

    # Reader mode. Two of the three providers never return an article body, so
    # the publisher's own page is read once per story and the text kept
    # forever. That costs no provider quota and is what gives a summary
    # something to condense.
    reader_enabled: bool = True
    reader_timeout_seconds: float = 8.0
    reader_max_bytes: int = 500_000
    reader_max_chars: int = 20_000
    # Kept above the summariser's own minimum (`providers/ai.MIN_SOURCE_CHARS`):
    # a body that is stored but too short to condense would be retried only
    # after `reader_retry_seconds`, so the reader reports "not enough text" for
    # a week over a page it already knows the answer to.
    reader_min_chars: int = 240
    # One process may only have this many publisher reads in flight, so a burst
    # of opened stories cannot turn the app into a crawler.
    reader_concurrency: int = 4
    # A page that yielded nothing is not tried again until this has passed.
    # Without it a paywalled site would be re-fetched on every single view.
    reader_retry_seconds: int = 60 * 60 * 24 * 7

    # Per-client request ceilings, per window. The free tiers are the scarce
    # resource, so abusive traffic is stopped before it can reach a provider.
    rate_limit_requests: int = 240
    rate_limit_window_seconds: int = 60
    # Sign-in is the one endpoint worth brute-forcing, and the one where a low
    # ceiling costs an honest reader nothing.
    rate_limit_auth_requests: int = 30
    rate_limit_auth_window_seconds: int = 300

    # A search is the one request that cannot be served from the page cache:
    # two people who type different words produce two cache keys, and each one
    # is an upstream call. The daily ceilings above are only 300 calls in
    # total, so a single client walking a dictionary through `/api/search`
    # would spend the day's allowance for everyone in under a minute. Each
    # client, signed in or not, therefore gets a daily allowance of *new*
    # searches; a query already in the cache is free and never counted.
    # Set the anonymous allowance to 0 to require an account to search at all.
    search_daily_queries_anonymous: int = 5
    search_daily_queries_user: int = 50
    # Bodies are a few hundred bytes of JSON; anything larger is an attack or a
    # mistake, and either way there is no reason to buffer it.
    max_request_bytes: int = 64 * 1024
    # Only correct because the API is reached through the Astro proxy on the
    # same host rather than exposed directly; the proxy is what sets this.
    trust_forwarded_for: bool = True

    # Shared with the Astro proxy, which sends it on every forwarded request.
    #
    # The two settings above and this one are one arrangement: taking the client
    # address from a header is only safe while the only thing that can reach
    # this service is the proxy that writes it. Split across two hosts — the
    # frontend on Vercel and this service on a public URL — that stops being
    # true, and a stranger who finds the hostname can send a fresh
    # `x-forwarded-for` with every request, count as a new client each time, and
    # spend the whole day's provider allowance in a minute. A secret cannot be
    # guessed, so the service answers the proxy and nobody else.
    #
    # Empty means the check is off, which is right for a deployment where the
    # API is on a private network and only the proxy can reach it at all.
    internal_api_secret: str = ""

    # How long a sign-in lasts. Sessions are rows in SQLite, so this is a
    # sliding-ish upper bound rather than a browser-only cookie expiry.
    session_ttl_days: int = 30
    # Set true once the app is served over HTTPS: without it the session cookie
    # would be sent over plaintext.
    session_cookie_secure: bool = False

    # Encrypts readers' AI provider keys at rest. Generate one with
    # `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
    # Until it is set, keys cannot be saved — which is better than saving them
    # in the clear, where any copy of news.db would hand them over.
    secret_key: str = ""

    # Text generation runs on the reader's own free-tier key, so the only real
    # limits are the provider's. Models are named here because they get retired.
    ai_groq_model: str = "llama-3.3-70b-versatile"
    # OpenRouter has dropped the Llama free tier; `:free` models churn, so this
    # is worth checking against https://openrouter.ai/api/v1/models when a
    # summary starts failing with a 404.
    #
    # Its reasoning pass is switched off on every request (see `providers/ai`),
    # because a model that thinks first spends the whole token budget and
    # answers with `content: null`. A non-reasoning model is still the right
    # default: it costs no tokens deciding how to decide.
    ai_openrouter_model: str = "google/gemma-4-31b-it:free"
    # OpenRouter tries these in order when the model above has no capacity, and
    # a `:free` model's capacity is a pool shared with every other anonymous
    # caller — so the first choice alone is a single point of failure. Two at
    # most: OpenRouter rejects a `models` array longer than three.
    ai_openrouter_fallbacks: str = (
        "google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3.5-lightning:free"
    )
    ai_gemini_model: str = "gemini-2.0-flash"
    # A summary takes tens of seconds to write; the feed's 15s would cut it off.
    ai_request_timeout_seconds: float = 75.0
    # Bodies run to thousands of words and only the opening matters for a
    # summary, so this bounds what is sent upstream.
    ai_max_input_chars: int = 6000
    # An article's summary never changes, so it is kept far longer than a feed
    # page — and it is shared, so a second reader costs nothing.
    ai_summary_ttl_seconds: int = 60 * 60 * 24 * 90

    cors_origins: str = "http://localhost:4321,http://127.0.0.1:4321"

    # Extra hosts to keep out of the feed, comma-separated. A host matches
    # itself and its subdomains. The built-in list in `services/quality.py`
    # always applies; this is for whatever the reader's own feed turns up.
    feed_blocked_domains: str = ""

    @model_validator(mode="after")
    def _check_production(self) -> "Settings":
        """Refuse to start a public deployment that is still in dev shape.

        Turning the app on without a host to run it on is easy; turning it on
        *completely* is the step that gets missed, and every omission here is
        one that fails quietly — a session cookie sent in the clear, provider
        keys encrypted with nothing. So `ENVIRONMENT=production` is a claim
        about the deployment, and an incomplete claim stops the process instead
        of serving strangers.
        """
        if self.environment != "production":
            return self

        problems: list[str] = []
        if not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true once the site is served over https")
        if not self.secret_key:
            problems.append("SECRET_KEY must be set to encrypt readers' AI keys at rest")

        if problems:
            raise ValueError(
                "ENVIRONMENT=production but the deployment is unfinished: "
                + "; ".join(problems)
            )
        return self

    @property
    def session_ttl_seconds(self) -> int:
        return self.session_ttl_days * 24 * 60 * 60

    @property
    def feed_blocked_host_list(self) -> frozenset[str]:
        return frozenset(
            host.strip().lower().removeprefix("www.")
            for host in self.feed_blocked_domains.split(",")
            if host.strip()
        )

    @property
    def database_file(self) -> Path:
        path = Path(self.database_path)
        return path if path.is_absolute() else BACKEND_DIR / path

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def ai_openrouter_fallback_list(self) -> list[str]:
        return [
            model.strip() for model in self.ai_openrouter_fallbacks.split(",") if model.strip()
        ][:2]


@lru_cache
def get_settings() -> Settings:
    return Settings()
