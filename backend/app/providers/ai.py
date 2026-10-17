"""The free text-generation tiers a reader can bring their own key to.

Three very different APIs behind one call. Two speak the OpenAI chat dialect
and one does not, which is the only reason there is a branch here at all.

Nothing in this module is billable to the deployment: the key belongs to the
reader, is stored against their account, and is spent only when they press
Summarise.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..schemas import Article, ArticleDetail, Summary

log = logging.getLogger(__name__)

GROQ = "groq"
OPENROUTER = "openrouter"
GEMINI = "gemini"

# Also the order of preference when a reader has more than one key stored:
# Groq's free tier is the fastest and has no daily cap worth worrying about.
PROVIDERS: tuple[str, ...] = (GROQ, OPENROUTER, GEMINI)

LABELS: dict[str, str] = {
    GROQ: "Groq",
    OPENROUTER: "OpenRouter",
    GEMINI: "Google Gemini",
}

_CHAT_URLS = {
    GROQ: "https://api.groq.com/openai/v1/chat/completions",
    OPENROUTER: "https://openrouter.ai/api/v1/chat/completions",
}
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Below this there is nothing to condense — most often a NewsAPI stub of two
# sentences. Summarising it would only restate it.
MIN_SOURCE_CHARS = 220

# How many other outlets' headlines may be offered as context. Enough to show
# that a story is widely reported, not enough to swamp the article itself.
MAX_COVERAGE = 6

_INSTRUCTIONS = (
    "You are a newsroom editor. A reader who has not seen this article has "
    "asked you for the essentials.\n"
    "Write one paragraph of at most 90 words: what happened, then why it "
    "matters. Then give three key facts, one per line, each starting with "
    '"- " and under 14 words.\n'
    "Use only the material below. Do not add background it does not contain, "
    'do not open with "This article", and do not use markdown headings.'
)


class AiError(RuntimeError):
    """A provider refused the request, or answered with something unusable."""

    def __init__(self, message: str, *, provider: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.provider = provider
        # The code the reader should see: a bad key is their mistake to fix,
        # a rate limit is theirs to wait out, anything else is on us.
        self.status_code = status_code


def build_prompt(
    article: ArticleDetail, *, max_chars: int, coverage: list[Article] | None = None
) -> str:
    """Everything the model gets: the instructions, the byline, the text.

    `coverage` is the other outlets' versions of the same story, as the
    clusterer found them. Only their headlines are offered — no free tier
    returns their bodies — and they are the difference between "a minister
    resigned" and knowing four newsrooms independently led on it.
    """
    source = source_text(article)
    if len(source) > max_chars:
        # Cut on a word boundary, and say so, rather than stopping mid-word.
        source = f"{source[:max_chars].rsplit(' ', 1)[0]}…"

    prompt = (
        f"{_INSTRUCTIONS}\n\n"
        f"Headline: {article.title}\n"
        f"Outlet: {article.source or 'unknown'}\n"
        f"Published: {article.published_at.strftime('%d %B %Y')}\n\n"
        f"Article:\n{source}"
    )

    others = _coverage_headlines(article, coverage)
    if others:
        prompt += "\n\nOther outlets reporting this, headline only:\n" + "\n".join(
            f"- {outlet}: {title}" for outlet, title in others
        )
    return prompt


def _coverage_headlines(
    article: ArticleDetail, coverage: list[Article] | None, *, limit: int = MAX_COVERAGE
) -> list[tuple[str, str]]:
    """One headline per other outlet, the story's own outlet excluded."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()

    for other in coverage or []:
        if other.id == article.id:
            continue
        outlet = (other.source or "").strip()
        title = (other.title or "").strip()
        # Two copies from the same outlet say nothing new, and a row with no
        # outlet name would read as an unattributed claim.
        if not outlet or not title or outlet.lower() in seen:
            continue
        seen.add(outlet.lower())
        rows.append((outlet, title))
        if len(rows) >= limit:
            break

    return rows


def source_text(article: ArticleDetail) -> str:
    """The article's own words, as opposed to its headline."""
    return (article.body or article.description or article.incipit or "").strip()


def parse_reply(reply: str) -> Summary:
    """Split a reply into the paragraph and the bullets that were asked for.

    Models mostly comply, but not always; a reply that is prose from end to end
    is still a usable summary, so it is accepted as one.
    """
    prose: list[str] = []
    bullets: list[str] = []

    for raw in reply.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _is_bullet(line):
            bullets.append(_strip_marker(line))
        else:
            prose.append(line)

    text = " ".join(prose).strip()
    if not text and bullets:
        # Nothing but bullets came back; better to show them than nothing.
        text = bullets[0]
        bullets = bullets[1:]

    return Summary(text=text[:1400], bullets=bullets[:5])


def _is_bullet(line: str) -> bool:
    if line[0] in "-*•–":
        return True
    # "1." / "1)" / "12." — models like a numbered list.
    head, dot, tail = line.partition(".")
    if not dot:
        head, dot, tail = line.partition(")")
    return bool(dot) and head.isdigit() and len(head) <= 2 and tail.startswith(" ")


def _strip_marker(line: str) -> str:
    """Drop the "-" or "1." that opened a bullet, keeping the fact."""
    text = line.lstrip("-*•– \t")
    head, dot, tail = text.partition(".")
    if not dot:
        head, dot, tail = text.partition(")")
    if dot and head.isdigit() and len(head) <= 2:
        return tail.strip()
    return text.strip()


def _failure(provider: str, status: int, model: str | None = None) -> AiError:
    label = LABELS.get(provider, provider)
    if status in (401, 403):
        return AiError(f"{label} rejected that key. Check it and save it again.", provider=provider, status_code=400)
    if status == 429:
        return AiError(
            f"{label} is rate limiting this key. Wait a moment and try again.",
            provider=provider,
            status_code=429,
        )
    if status == 402:
        return AiError(
            f"{label} reports no credit left on this key.",
            provider=provider,
            status_code=402,
        )
    if status == 404:
        # OpenRouter answers 404 both for a model id it has retired and for one
        # whose free providers have all withdrawn. Either way it is the
        # deployment's model setting at fault, not the reader's key, and
        # "(HTTP 404)" alone gives nobody anything to act on.
        which = f"`{model}`" if model else "the model this deployment is set to"
        return AiError(
            f"{label} no longer serves {which}. Try another provider, or point "
            f"the deployment at a model {label} still carries.",
            provider=provider,
            status_code=502,
        )
    return AiError(f"{label} could not answer (HTTP {status}).", provider=provider)


# A `:free` model is served from a pool the provider shares between every
# anonymous caller, and that pool saturates constantly. OpenRouter says so in
# the body — `limit_source: upstream_provider_shared_pool` — and distinguishes it
# from the reader's own key having hit its limit.
_POOL_MARKER = "upstream_provider_shared_pool"
# The pools free up within seconds, so one short wait is usually the whole
# difference between a summary and a complaint.
_POOL_RETRY_SECONDS = 2.5


def _shared_pool(response: httpx.Response) -> bool:
    """True when a 429 is the model's shared pool rather than the reader's key."""
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return False
    metadata = error.get("metadata") or {}
    if metadata.get("limit_source") == _POOL_MARKER:
        return True
    # The marker is also spelled out in prose for callers that only read `raw`.
    return "temporarily rate-limited upstream" in str(metadata.get("raw", ""))


def _pool_failure(provider: str, model: str | None) -> AiError:
    label = LABELS.get(provider, provider)
    which = f"`{model}`" if model else "that model"
    return AiError(
        f"{label} has no free capacity for {which} right now — its free pool is "
        f"shared and currently busy. Try again in a minute.",
        provider=provider,
        status_code=429,
    )


def _per_request(timeout: float | None) -> dict[str, Any]:
    """Overrides passed to `client.post`, which otherwise carries the feed's
    much shorter timeout — writing a summary takes tens of seconds."""
    return {} if timeout is None else {"timeout": timeout}


async def _chat(
    client: httpx.AsyncClient,
    *,
    provider: str,
    url: str,
    model: str,
    api_key: str,
    prompt: str,
    timeout: float | None = None,
    alternates: list[str] | None = None,
) -> str:
    """The OpenAI-shaped call, which Groq and OpenRouter both accept.

    `alternates` are handed to OpenRouter's router, which tries them in order
    when the first has no capacity. A `:free` model draws on a pool shared with
    every other anonymous caller, so a single slug is a single point of failure.
    """
    headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
    if provider == OPENROUTER:
        # OpenRouter attributes free-tier traffic from these two headers.
        headers["x-title"] = "The Athenaeum"

    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 700,
    }
    if provider == OPENROUTER:
        # Summarising is instruction-following, not a puzzle, and a model that
        # thinks first spends the entire token budget doing so and answers with
        # `content: null` — which is the 500 this guarded against. Several free
        # models reason by default without advertising it, so this is asked for
        # explicitly rather than inferred from the model card.
        payload["reasoning"] = {"enabled": False}
    if alternates:
        # OpenRouter routes across `models` in the order given and falls through
        # on its own, so a saturated pool costs a few seconds rather than the
        # reader's summary. It accepts three at most.
        payload["models"] = [model, *alternates][:3]
    else:
        payload["model"] = model

    request = {"headers": headers, "json": payload}

    response = await client.post(url, **request, **_per_request(timeout))

    if response.status_code == 429 and _shared_pool(response):
        # The model's free pool, not the reader's key. Waiting briefly is the
        # documented remedy and usually enough of one; a reader who has just
        # pressed Summarise would rather wait three seconds than be told to
        # change a key that is perfectly good.
        log.info("%s free pool busy; retrying %s once", LABELS[provider], model)
        await asyncio.sleep(_POOL_RETRY_SECONDS)
        response = await client.post(url, **request, **_per_request(timeout))
        if response.status_code == 429 and _shared_pool(response):
            raise _pool_failure(provider, model)

    if response.status_code >= 400:
        raise _failure(provider, response.status_code, model)

    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AiError(f"{LABELS[provider]} returned nothing usable.", provider=provider) from exc

    if not content or not content.strip():
        # A reasoning model can spend the entire token budget thinking and hand
        # back `content: null`, and a refusal looks the same. Either way there
        # is no summary; saying so beats failing on the None further down.
        raise AiError(
            f"{LABELS[provider]} returned an empty summary — the model may have "
            f"run out of tokens before answering.",
            provider=provider,
        )
    return content


async def _gemini(
    client: httpx.AsyncClient,
    *,
    model: str,
    api_key: str,
    prompt: str,
    timeout: float | None = None,
) -> str:
    response = await client.post(
        _GEMINI_URL.format(model=model),
        headers={"x-goog-api-key": api_key, "content-type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700},
        },
        **_per_request(timeout),
    )
    if response.status_code >= 400:
        raise _failure(GEMINI, response.status_code, model)

    try:
        parts = response.json()["candidates"][0]["content"]["parts"]
        return "".join(part.get("text", "") for part in parts)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # A prompt blocked by a safety filter lands here, with no candidates.
        raise AiError("Gemini returned nothing usable for this story.", provider=GEMINI) from exc


async def ask(
    client: httpx.AsyncClient,
    *,
    provider: str,
    model: str,
    api_key: str,
    prompt: str,
    timeout: float | None = None,
    alternates: list[str] | None = None,
) -> Summary:
    if provider == GEMINI:
        reply = await _gemini(
            client, model=model, api_key=api_key, prompt=prompt, timeout=timeout
        )
    else:
        reply = await _chat(
            client,
            provider=provider,
            url=_CHAT_URLS[provider],
            model=model,
            api_key=api_key,
            prompt=prompt,
            timeout=timeout,
            alternates=alternates,
        )

    summary = parse_reply(reply)
    if not summary.text:
        raise AiError(f"{LABELS[provider]} returned an empty summary.", provider=provider)
    return summary
