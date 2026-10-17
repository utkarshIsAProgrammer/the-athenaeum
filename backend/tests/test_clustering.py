"""Story clustering: what merges, what must not, and which copy leads."""

from __future__ import annotations

from datetime import datetime, timezone

from app.schemas import Article
from app.services.clustering import cluster

WHEN = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def article(title: str, *, url: str, source: str, image: bool = False) -> Article:
    return Article(
        id=url,
        title=title,
        url=f"https://{url}",
        image_url="https://img/1.jpg" if image else None,
        source=source,
        published_at=WHEN,
        provider="newsdata",
    )


def test_same_story_from_two_outlets_folds_onto_one_lead() -> None:
    lead = article(
        "Tesla unveils the flying car Roadster",
        url="a",
        source="Reuters",
        image=True,
    )
    other = article(
        "Tesla's flying car Roadster stuns in demo",
        url="b",
        source="The Verge",
    )

    kept, clusters, coverage = cluster([lead, other])

    assert [item.id for item in kept] == ["a"]
    assert len(clusters) == 1
    assert clusters[0].lead_id == "a"
    assert clusters[0].count == 2
    assert clusters[0].sources == ["Reuters", "The Verge"]
    # The thread the story page renders: both copies, lead first.
    assert [item.id for item in coverage["a"]] == ["a", "b"]


def test_the_copy_with_an_image_leads_even_when_it_arrives_second() -> None:
    plain = article("Tesla unveils the flying car Roadster", url="a", source="Reuters")
    illustrated = article(
        "Tesla's flying car Roadster stuns in demo",
        url="b",
        source="The Verge",
        image=True,
    )

    kept, clusters, _coverage = cluster([plain, illustrated])

    assert [item.id for item in kept] == ["b"]
    assert clusters[0].lead_id == "b"


def test_different_stories_about_one_company_stay_apart() -> None:
    # Both are about Apple; neither is the same story.
    first = article("Apple unveils the HomeView smart hub", url="a", source="MacRumors")
    second = article("Apple's iPhone Duo case sticks with adhesive", url="b", source="CNET")

    kept, clusters, coverage = cluster([first, second])

    assert len(kept) == 2
    assert clusters == []
    assert coverage == {}


def test_generic_headlines_are_not_merged_on_their_shared_words() -> None:
    # "live updates" is all these two share, and it is a stopword-shaped signal.
    first = article("Live updates on the storm in Florida", url="a", source="A")
    second = article("Live updates from the market in London", url="b", source="B")

    kept, _clusters, _coverage = cluster([first, second])

    assert len(kept) == 2


def test_a_single_article_yields_no_clusters() -> None:
    kept, clusters, coverage = cluster([article("Anything at all here", url="a", source="A")])

    assert len(kept) == 1
    assert clusters == []
    assert coverage == {}


def test_a_chain_of_matches_collapses_into_one_group() -> None:
    # A matches B, B matches C, but A and C share nothing — union-find still
    # has to produce a single cluster rather than two overlapping ones.
    a = article("Championship final ends in dramatic penalty shootout", url="a", source="A")
    b = article("Championship final ends in dramatic penalty drama", url="b", source="B")
    c = article("Dramatic penalty drama decides the final", url="c", source="C", image=True)

    kept, clusters, coverage = cluster([a, b, c])

    assert len(kept) == 1
    assert len(clusters) == 1
    assert clusters[0].count == 3
    assert clusters[0].lead_id == "c"
    # Every copy reaches the thread, not just the two that share wording.
    assert {item.id for item in coverage["c"]} == {"a", "b", "c"}
