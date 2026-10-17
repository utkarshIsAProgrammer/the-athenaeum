"""Rules that keep the feed worth reading.

The free tiers syndicate very widely — the same wire copy is republished by
hundreds of sites, and the APIs happily index the republishers. Together with a
handful of rows that point at an aggregator rather than a newsroom, that is
enough to make a fifty-item page feel like a scraper's output.

Everything here is decided from the payload alone, so a drop costs no upstream
request. The check is deliberately conservative: a story is only dropped when its
link cannot lead to a publisher, never because the story seems dull.
"""

from __future__ import annotations

from ..providers.base import normalize_url
from ..schemas import Article

# Hosts that are structurally not a publisher. The API vendors' own domains are
# here because they turn up as `source` on badly-formed rows, and the rest are
# redirectors: the reader arrives at the publisher anyway, so carrying the hop
# adds nothing but a click.
ALWAYS_BLOCKED_HOSTS = frozenset(
    {
        "news.google.com",
        "newsapi.org",
        "newsdata.io",
        "freenewsapi.io",
        "flipboard.com",
        "flip.it",
    }
)


def host_of(url: str) -> str:
    """The host of a URL, lower-cased and without `www.`; empty when there is none."""
    normalized = normalize_url(url)
    return normalized.split("/", 1)[0] if normalized else ""


def matches_host(host: str, entries: frozenset[str]) -> bool:
    """Host equality, or a subdomain of an entry — what a domain list means.

    Public because `services/reader.py` applies the same list to the pages it
    is asked to fetch, and a second copy of this rule is a second place to get
    it wrong.
    """
    return any(host == entry or host.endswith(f".{entry}") for entry in entries)


def is_blocked(article: Article, *, blocked_hosts: frozenset[str] = frozenset()) -> bool:
    """True when this story should not be shown or remembered at all."""
    host = host_of(article.url)
    # A card with nowhere to send the reader is not worth the slot; every
    # provider is supposed to supply a link, so this only catches malformed rows.
    if "." not in host:
        return True
    return matches_host(host, ALWAYS_BLOCKED_HOSTS) or matches_host(host, blocked_hosts)


def keep(articles: list[Article], *, blocked_hosts: frozenset[str] = frozenset()) -> list[Article]:
    """The subset of `articles` that is fit to serve."""
    return [article for article in articles if not is_blocked(article, blocked_hosts=blocked_hosts)]
