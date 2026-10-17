"""Groups the same story as told by different outlets.

Providers hand over a flat list, and the aggregator's URL/title de-duplication
only catches literal repeats. The interesting case is the story that five
newsrooms each wrote up in their own words — five near-identical cards in the
feed. This module folds those into one lead article plus a note of who else
covered it.

The match is deliberately conservative: headlines are compared as sets of
significant words and merged only when they overlap heavily, so "Apple unveils
the iPhone Duo" and "Apple's iPhone Duo case sticks on with adhesive" stay
apart even though both are about Apple and the iPhone.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..schemas import Article, StoryCluster

_WORD = re.compile(r"[a-z0-9]+")

# Words that carry no story-specific meaning in a headline.
STOPWORDS = frozenset(
    """
    a about after against all also an and another are as at be been before being
    between both but by can could day days did do does doing down during each
    even every first for from further get gets getting had has have having he
    her here hers him his how however i if in into is it its itself just like
    made make makes making many may me might more most much must my new news no
    nor not now of off on once one only or other others our out over own said
    same says she should since so some such than that the their them then there
    these they this those through to too under until up upon us use used using
    very was way we well were what when where which while who why will with
    within without would year years you your
    """.split()
)

MIN_TOKEN_LENGTH = 3

# Two headlines must share at least this many significant words before they
# are even considered, and then overlap at least this much to be merged.
MIN_SHARED_WORDS = 2
SIMILARITY_THRESHOLD = 0.55

# Which sources a cluster reports, capped so a syndicated wire story cannot
# push a hundred outlet names into the payload.
MAX_SOURCES = 6


def _tokens(title: str) -> set[str]:
    return {
        word
        for word in _WORD.findall(title.lower())
        if len(word) >= MIN_TOKEN_LENGTH and word not in STOPWORDS
    }


def richness(article: Article) -> int:
    """Score used to pick the best copy of a story.

    A card image is worth more than anything else, which is what makes
    NewsData's and FreeNewsApi's copies win over NewsAPI's.
    """
    score = 0
    if article.image_url:
        score += 3
    if article.description:
        score += 1
    if article.source:
        score += 1
    if article.source_icon:
        score += 1
    if article.author:
        score += 1
    if article.sentiment:
        score += 1
    return score


class _Union:
    """Union-find, so a chain of pairwise matches collapses to one group."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


def cluster(
    articles: list[Article],
) -> tuple[list[Article], list[StoryCluster], dict[str, list[Article]]]:
    """Return `(articles, clusters, coverage)`.

    `articles` keeps its original order minus the absorbed copies; `clusters`
    describes each group of two or more so a card can name the other outlets.
    `coverage` is keyed by lead id and holds every copy in the group, newest
    first — the feed payload only needs the summary, but a story page wants the
    other newsrooms' own headlines, which is more than `StoryCluster` carries.
    """
    if len(articles) < 2:
        return list(articles), [], {}

    tokens = [_tokens(article.title) for article in articles]

    index: dict[str, list[int]] = defaultdict(list)
    for position, words in enumerate(tokens):
        for word in words:
            index[word].append(position)

    groups = _Union(len(articles))
    for position, words in enumerate(tokens):
        if len(words) < MIN_SHARED_WORDS:
            continue
        shared = Counter()
        for word in words:
            for other in index[word]:
                if other != position:
                    shared[other] += 1
        for other, count in shared.items():
            if other <= position or count < MIN_SHARED_WORDS:
                continue
            overlap = count / min(len(words), len(tokens[other]))
            if overlap >= SIMILARITY_THRESHOLD:
                groups.union(position, other)

    members: dict[int, list[int]] = defaultdict(list)
    for position in range(len(articles)):
        members[groups.find(position)].append(position)

    absorbed: set[int] = set()
    clusters: list[StoryCluster] = []
    coverage: dict[str, list[Article]] = {}

    for positions in members.values():
        if len(positions) < 2:
            continue
        # The richest copy leads; the rest are dropped but recorded.
        positions.sort(key=lambda position: richness(articles[position]), reverse=True)
        lead = articles[positions[0]]
        others = [articles[position] for position in positions[1:]]
        absorbed.update(positions[1:])

        sources: list[str] = []
        for article in [lead, *others]:
            name = article.source
            if name and name not in sources:
                sources.append(name)

        clusters.append(
            StoryCluster(
                lead_id=lead.id,
                count=len(positions),
                sources=sources[:MAX_SOURCES],
                titles=[article.title for article in others],
            )
        )
        coverage[lead.id] = sorted(
            [lead, *others], key=lambda article: article.published_at, reverse=True
        )

    kept = [article for position, article in enumerate(articles) if position not in absorbed]
    return kept, clusters, coverage
