"""The reader's own AI keys, and the summaries they buy.

Nothing here costs the deployment anything: the key belongs to the reader, is
stored encrypted against their account, and is spent only when they press
Summarise. Summaries are cached per article, so the second reader to ask about
the same story with the same provider pays nothing at all.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response

from ..config import Settings, get_settings
from ..crypto import SecretsUnavailable, decrypt, encrypt
from ..db import Database
from ..dependencies import get_aggregator, get_database, require_user
from ..providers import ai
from ..schemas import (
    AiKeyInput,
    AiKeyState,
    AiKeysResponse,
    SummaryRequest,
    SummaryResponse,
    User,
)
from ..services import NewsAggregator

router = APIRouter(prefix="/ai", tags=["ai"])

# The last four characters tell two keys apart without being enough to use one,
# which is all the account page needs to show.
HINT_CHARS = 4

_NO_KEY = "Add a free AI key on your account page to summarise stories."


def _hint(key: str) -> str:
    return f"…{key[-HINT_CHARS:]}" if len(key) > HINT_CHARS else "…"


def _model(provider: str, settings: Settings) -> str:
    if provider == ai.GROQ:
        return settings.ai_groq_model
    if provider == ai.OPENROUTER:
        return settings.ai_openrouter_model
    return settings.ai_gemini_model


def _stored_key(user_id: str, provider: str, database: Database, settings: Settings) -> str:
    """Decrypt one reader's key, or explain why it cannot be used."""
    ciphertext = database.provider_key_get(user_id, provider)
    if ciphertext is None:
        raise HTTPException(
            status_code=409, detail=f"Save a {ai.LABELS[provider]} key on your account page first."
        )
    try:
        return decrypt(settings.secret_key, ciphertext)
    except SecretsUnavailable as exc:
        # 503, not 500: the deployment is missing SECRET_KEY, which an operator
        # can fix and the reader cannot.
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _choose_provider(
    requested: str | None, user_id: str, database: Database
) -> str:
    """Which provider answers this request.

    `ai.PROVIDERS` doubles as the preference order, so a reader who has stored
    several keys gets the fastest one unless they name another.
    """
    stored = set(database.provider_hints(user_id))

    if requested is None:
        if not stored:
            raise HTTPException(status_code=409, detail=_NO_KEY)
        return next(provider for provider in ai.PROVIDERS if provider in stored)

    if requested not in ai.PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown AI provider.")
    if requested not in stored:
        raise HTTPException(
            status_code=409, detail=f"Save a {ai.LABELS[requested]} key on your account page first."
        )
    return requested


@router.get("/keys", response_model=AiKeysResponse)
async def list_keys(
    user: User = Depends(require_user), database: Database = Depends(get_database)
) -> AiKeysResponse:
    """Which providers are configured. The key itself never comes back."""
    hints = database.provider_hints(user.id)
    return AiKeysResponse(
        keys=[
            AiKeyState(
                provider=provider,
                label=ai.LABELS[provider],
                configured=provider in hints,
                hint=hints.get(provider),
            )
            for provider in ai.PROVIDERS
        ]
    )


@router.put("/keys/{provider}", response_model=AiKeyState)
async def save_key(
    provider: str,
    payload: AiKeyInput,
    user: User = Depends(require_user),
    database: Database = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> AiKeyState:
    """Store a provider key, encrypted with the deployment's secret."""
    if provider not in ai.PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown AI provider.")

    key = payload.key.strip()
    if not key:
        raise HTTPException(status_code=422, detail="That does not look like a key.")

    try:
        ciphertext = encrypt(settings.secret_key, key)
    except SecretsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    hint = _hint(key)
    database.provider_key_set(user.id, provider, ciphertext, hint)
    return AiKeyState(provider=provider, label=ai.LABELS[provider], configured=True, hint=hint)


@router.delete("/keys/{provider}", status_code=204)
async def delete_key(
    provider: str,
    user: User = Depends(require_user),
    database: Database = Depends(get_database),
) -> Response:
    if not database.provider_key_delete(user.id, provider):
        raise HTTPException(status_code=404, detail="No key stored for that provider.")
    return Response(status_code=204)


@router.post("/summarise", response_model=SummaryResponse)
async def summarise(
    request: SummaryRequest,
    user: User = Depends(require_user),
    database: Database = Depends(get_database),
    news: NewsAggregator = Depends(get_aggregator),
    settings: Settings = Depends(get_settings),
) -> SummaryResponse:
    """Condense one article with the reader's own key.

    The order matters: the article is resolved *before* the provider so that a
    story we can no longer find is a 404 rather than a confusing demand for a
    key, and the cache is consulted before the source is measured so a cached
    summary still answers for a story whose body has since been trimmed.

    Resolving the article is also what recovers its text: `news.article` reads
    the publisher's own page when no provider supplied a body, so by the time
    the source is measured there is usually something to condense.
    """
    article = await news.article(request.article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="That article is no longer available.")

    provider = _choose_provider(request.provider, user.id, database)
    label = ai.LABELS[provider]

    if not request.refresh:
        cached = database.summary_get(article.id, provider)
        if cached is not None:
            return SummaryResponse(provider=provider, label=label, summary=cached, cached=True)

    source = ai.source_text(article)
    if len(source) < ai.MIN_SOURCE_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                "There is not enough text here to summarise — the publisher's page "
                "could not be read, or sits behind a paywall. Open the story at its "
                "publisher instead."
            ),
        )

    api_key = _stored_key(user.id, provider, database, settings)
    thread = database.thread_for(article.id)
    prompt = ai.build_prompt(
        article,
        max_chars=settings.ai_max_input_chars,
        coverage=thread.coverage if thread else None,
    )

    try:
        summary = await ai.ask(
            news.require_client(),
            provider=provider,
            model=_model(provider, settings),
            api_key=api_key,
            prompt=prompt,
            timeout=settings.ai_request_timeout_seconds,
            # Only OpenRouter routes across several models; the other two are
            # handed one and would ignore the list.
            alternates=(
                settings.ai_openrouter_fallback_list if provider == ai.OPENROUTER else None
            ),
        )
    except ai.AiError as exc:
        # `status_code` is set by the provider module: a bad key is the
        # reader's to fix, a rate limit theirs to wait out.
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"{label} could not be reached."
        ) from exc

    database.summary_set(article.id, provider, summary, settings.ai_summary_ttl_seconds)
    return SummaryResponse(provider=provider, label=label, summary=summary)
