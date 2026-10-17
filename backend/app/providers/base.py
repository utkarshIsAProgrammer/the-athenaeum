"""Shared normalisation helpers for the upstream news providers."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from urllib.parse import urlsplit

NEWSAPI = "newsapi"
NEWSDATA = "newsdata"
FREENEWSAPI = "freenewsapi"

# Both free tiers replace paid-only fields with a literal sentinel string
# rather than omitting them, so they have to be filtered out by hand.
PLACEHOLDER_TEXT = {
    "only available in paid plans",
    "only available in corporate plans",
    "[removed]",
    "removed",
}

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "cmpid",
    "ocid",
    "igshid",
    "at_medium",
    "at_campaign",
}


class ProviderError(RuntimeError):
    """An upstream provider refused or failed a request."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        quota_exhausted: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.quota_exhausted = quota_exhausted


def clean_text(value: object) -> str | None:
    """Trim a provider string and drop known placeholder sentinels."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        joined = ", ".join(part for part in (clean_text(item) for item in value) if part)
        return joined or None
    text = str(value).strip()
    if not text or text.lower() in PLACEHOLDER_TEXT:
        return None
    return text


# A placeholder thumbnail is worse than no thumbnail at all: the card reserves a
# slot for it, the reader learns nothing from it, and a page of grey rectangles
# reads as machine-built. Providers do not mark these, so they are recognised by
# file name — the free tiers emit `default.jpg` and `1x1.gif` often enough to be
# worth catching.
_PLACEHOLDER_IMAGE_NAMES = frozenset(
    {
        "1x1.gif",
        "1x1.jpg",
        "1x1.png",
        "blank.gif",
        "blank.png",
        "default.gif",
        "default.jpg",
        "default.jpeg",
        "default.png",
        "default.webp",
        "default-image.jpg",
        "default-image.png",
        "defaultimage.jpg",
        "defaultimage.png",
        "missing.jpg",
        "missing.png",
        "no-image.jpg",
        "no-image.png",
        "no_image.jpg",
        "no_image.png",
        "noimage.jpg",
        "noimage.png",
        "pixel.gif",
        "pixel.png",
        "placeholder.gif",
        "placeholder.jpg",
        "placeholder.png",
        "placeholder.webp",
        "spacer.gif",
        "spacer.png",
        "transparent.gif",
        "transparent.png",
    }
)

# Anything in a directory by one of these names is a placeholder too, whatever
# the file is called — `/assets/placeholder/hero.jpg` is not a photograph.
_PLACEHOLDER_IMAGE_SEGMENTS = frozenset(
    {"placeholder", "no-image", "no_image", "default-image", "defaultimage"}
)


def _image_path_segments(url: str) -> list[str]:
    path = url.split("?", 1)[0].split("#", 1)[0]
    return [segment for segment in path.split("/") if segment]


def clean_image(value: object) -> str | None:
    """A provider thumbnail, or None when it would only be a grey rectangle.

    `data:` URIs and protocol-relative values are dropped too: neither can be
    handed to an `<img>` on this origin without being resolved first.
    """
    raw = clean_text(value)
    if not raw:
        return None

    lowered = raw.lower()
    if not lowered.startswith(("http://", "https://")):
        return None

    segments = _image_path_segments(lowered)
    if segments and segments[-1] in _PLACEHOLDER_IMAGE_NAMES:
        return None
    if any(segment in _PLACEHOLDER_IMAGE_SEGMENTS for segment in segments[:-1]):
        return None
    return raw


def normalize_url(value: object) -> str:
    """Host + path + non-tracking query, so the same story de-duplicates."""
    raw = clean_text(value)
    if not raw:
        return ""

    parts = urlsplit(raw)
    if parts.netloc:
        host = parts.netloc.lower()
        path, query = parts.path, parts.query
    else:
        # A bare "example.com/story" is not parsed as a netloc by urlsplit, and
        # the path it leaves behind has lost its leading slash.
        host, _, remainder = raw.partition("/")
        host = host.lower()
        bare_path, _, query = remainder.partition("?")
        path = f"/{bare_path}"

    host = host.removeprefix("www.")
    path = path.rstrip("/")
    kept = [
        pair
        for pair in query.split("&")
        if pair and not _is_tracking(pair.split("=", 1)[0].lower())
    ]

    normalized = f"{host}{path}"
    return f"{normalized}?{'&'.join(kept)}" if kept else normalized


def _is_tracking(param: str) -> bool:
    return param in _TRACKING_PARAMS or param.startswith("utm_")


def article_id(url: object, title: object) -> str:
    """Stable id derived from the canonical URL, falling back to the title.

    Because the id is a pure function of the URL, an article syndicated to the
    same address by both providers collapses onto one entry for free.
    """
    basis = normalize_url(url) or (clean_text(title) or "").lower()
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# Separators FreeNewsApi uses to bolt the outlet onto the headline.
_TITLE_SUFFIXES = (" - ", " | ", " — ", " – ")


def strip_source_suffix(title: str, source: object) -> str:
    """Drop FreeNewsApi's trailing " - Publisher" from a headline.

    Every one of its titles carries the outlet name, while the story arrives
    from NewsData and NewsAPI without it. Left in place the suffix would
    defeat title de-duplication and read as noise in a card.
    """
    name = clean_text(source)
    if not name:
        return title
    for separator in _TITLE_SUFFIXES:
        suffix = f"{separator}{name}"
        if title.endswith(suffix) and len(title) > len(suffix):
            return title[: -len(suffix)].strip()
    return title


def parse_datetime(value: object) -> datetime:
    """Parse the several timestamp dialects the providers emit, defaulting to now."""
    text = clean_text(value)
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return datetime.now(timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
