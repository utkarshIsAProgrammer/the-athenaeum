"""Feed quality: placeholders dropped, dead links dropped, real stories kept."""

from __future__ import annotations

from datetime import datetime, timezone

from app.providers import newsapi, newsdata
from app.providers.base import clean_image
from app.schemas import Article
from app.services.quality import host_of, is_blocked, keep

WHEN = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def article(url: str, *, title: str = "A story worth reading", image: str | None = None) -> Article:
    return Article(id=url, title=title, url=url, image_url=image, published_at=WHEN, provider="newsdata")


# ------------------------------------------------------------ thumbnails


def test_a_real_thumbnail_survives() -> None:
    assert clean_image("https://cdn.thepaper.example/2026/10/storm.jpg") == (
        "https://cdn.thepaper.example/2026/10/storm.jpg"
    )


def test_a_stock_placeholder_is_dropped() -> None:
    assert clean_image("https://thepaper.example/default.jpg") is None
    assert clean_image("https://thepaper.example/img/1x1.gif") is None
    assert clean_image("https://thepaper.example/assets/placeholder.png") is None
    assert clean_image("https://thepaper.example/no-image.jpg") is None


def test_something_that_is_not_a_fetchable_url_is_dropped() -> None:
    assert clean_image("data:image/gif;base64,R0lGODlhAQABAAAAACw=") is None
    assert clean_image("//cdn.thepaper.example/storm.jpg") is None
    assert clean_image("") is None
    assert clean_image(None) is None


def test_the_placeholder_rule_is_applied_where_the_thumbnails_arrive() -> None:
    from_newsdata = newsdata._to_article(
        {
            "title": "Storm closes the harbour",
            "link": "https://thepaper.example/storm",
            "image_url": "https://thepaper.example/default.jpg",
        }
    )
    assert from_newsdata is not None
    assert from_newsdata.image_url is None

    from_newsapi = newsapi._to_article(
        {
            "title": "Storm closes the harbour",
            "url": "https://thepaper.example/storm",
            "urlToImage": "https://thepaper.example/placeholder.jpg",
        },
        category=None,
    )
    assert from_newsapi is not None
    assert from_newsapi.image_url is None


# ---------------------------------------------------------------- hosts


def test_host_is_read_without_scheme_or_www() -> None:
    assert host_of("https://www.ThePaper.example/storm?a=b") == "thepaper.example"
    assert host_of("thepaper.example/storm") == "thepaper.example"
    assert host_of("") == ""


def test_an_aggregator_link_is_refused() -> None:
    assert is_blocked(article("https://news.google.com/rss/articles/CBMi"))
    assert is_blocked(article("https://flip.it/abc123"))


def test_a_configured_host_takes_its_subdomains_with_it() -> None:
    blocked = frozenset({"badsyndicator.com"})

    assert is_blocked(article("https://badsyndicator.com/a"), blocked_hosts=blocked)
    assert is_blocked(article("https://news.badsyndicator.com/a"), blocked_hosts=blocked)
    assert not is_blocked(article("https://goodsyndicator.com/a"), blocked_hosts=blocked)
    # A suffix is not a subdomain.
    assert not is_blocked(article("https://notbadsyndicator.com/a"), blocked_hosts=blocked)


def test_nothing_is_blocked_by_default() -> None:
    assert not is_blocked(article("https://thepaper.example/storm"))
    assert len(keep([article("https://thepaper.example/storm")])) == 1


def test_a_link_that_cannot_lead_anywhere_is_refused() -> None:
    assert is_blocked(article(""))
    assert is_blocked(article("the harbour"))
