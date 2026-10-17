from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class QuotaState(BaseModel):
    """Remaining daily allowance for one upstream provider."""

    provider: str
    used: int
    limit: int
    remaining: int


class Article(BaseModel):
    """Normalised article shape shared by every provider.

    Neither free tier returns a usable article body — NewsAPI truncates to
    ~200 characters and NewsData answers with the literal string
    "ONLY AVAILABLE IN PAID PLANS" — so there is deliberately no `content`
    field. The client renders `description` and links out to `url`.
    """

    id: str
    title: str
    description: str | None = None
    url: str
    image_url: str | None = None
    source: str | None = None
    source_icon: str | None = None
    author: str | None = None
    published_at: datetime
    categories: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    sentiment: str | None = None
    provider: str


class Bookmark(Article):
    bookmarked_at: datetime


class ArticleDetail(Article):
    """Reader-mode payload.

    Kept separate from `Article` so a multi-thousand-word `body` is never
    carried inside a fifty-item feed response.
    """

    body: str | None = None
    incipit: str | None = None
    authors: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class StoryCluster(BaseModel):
    """One story as several outlets told it, collapsed onto its best copy.

    Only the lead survives in `FeedResponse.articles`; this describes what was
    folded into it so a card can say "also covered by 4 outlets".
    """

    lead_id: str
    count: int
    sources: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)


class StoryThread(BaseModel):
    """One story as several newsrooms told it, kept so it can be reopened later.

    `StoryCluster` only survives inside the cached feed page that produced it,
    which is evicted within the hour. A thread is the durable form: it carries
    every copy the clusterer folded together, newest first, so a story page can
    put the outlets' own headlines side by side — the closest thing to a second
    opinion that three wire services allow, and it costs no upstream quota.
    """

    lead_id: str
    count: int
    # `sources` includes the lead's own outlet; `coverage` includes the lead.
    sources: list[str] = Field(default_factory=list)
    coverage: list[Article] = Field(default_factory=list)


class ThreadResponse(BaseModel):
    """`thread` is null for the many stories only one outlet ran.

    Soft rather than a 404 because the story page asks on every article, and
    most articles genuinely have no thread.
    """

    thread: StoryThread | None = None


class FeedMeta(BaseModel):
    count: int
    cached: bool
    stale: bool
    providers: list[str]
    quotas: list[QuotaState]
    next_cursor: str | None = None
    has_more: bool = False
    clusters: list[StoryCluster] = Field(default_factory=list)


class FeedResponse(BaseModel):
    articles: list[Article]
    meta: FeedMeta


class BookmarksResponse(BaseModel):
    bookmarks: list[Bookmark]


class Category(BaseModel):
    slug: str
    label: str


class CategoriesResponse(BaseModel):
    categories: list[Category]


# The two things a reader can ask to see more of. A topic is one of the
# frontend's canonical categories, so it can be handed straight back to
# `/api/feed?category=`; a source is an outlet's name as the providers print it.
FollowKind = Literal["topic", "source"]


class Follow(BaseModel):
    """A subject or an outlet a reader asked to see more of.

    `label` is resolved when the response is built rather than stored, so a
    renamed category does not leave an old name behind in every reader's list.
    """

    kind: FollowKind
    value: str
    label: str


class FollowsResponse(BaseModel):
    follows: list[Follow] = Field(default_factory=list)


class FollowInput(BaseModel):
    kind: FollowKind
    value: str = Field(min_length=1, max_length=120)


class ArchiveDay(BaseModel):
    """One day the reading room happens to hold stories for."""

    day: str
    count: int


class ArchiveIndexResponse(BaseModel):
    days: list[ArchiveDay] = Field(default_factory=list)


class ArchiveDayResponse(BaseModel):
    day: str
    articles: list[Article] = Field(default_factory=list)
    total: int = 0


class TrendingStory(BaseModel):
    """A story picked out because several outlets ran it."""

    id: str
    title: str
    url: str
    source: str | None = None
    image_url: str | None = None
    published_at: datetime
    sources: list[str] = Field(default_factory=list)


class TrendingTopic(BaseModel):
    slug: str
    label: str
    count: int


class TrendingResponse(BaseModel):
    stories: list[TrendingStory]
    topics: list[TrendingTopic]


class HealthResponse(BaseModel):
    ok: bool
    service: str
    providers: list[str]
    quotas: list[QuotaState]


class User(BaseModel):
    """A registered reader. The password hash never leaves the database."""

    id: str
    email: str
    created_at: datetime


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    # Bounded so a 10 MiB "password" cannot be used to burn CPU in scrypt.
    password: str = Field(min_length=8, max_length=256)


class MeResponse(BaseModel):
    """`user` is null rather than a 401 so the shell can render signed-out.

    Every page asks who is signed in, so an error here would be a console full
    of failures on the most common path.
    """

    user: User | None = None


class Summary(BaseModel):
    """A story compressed by the reader's own key.

    Kept as a paragraph plus bullets rather than free text, because that is the
    shape the reader renders and the shape every prompt asks for.
    """

    text: str
    bullets: list[str] = Field(default_factory=list)


class AiKeyState(BaseModel):
    """Whether a provider key is stored, and enough of it to recognise it.

    The key itself never comes back: `hint` is the last four characters, which
    is enough to tell two keys apart and not enough to use one.
    """

    provider: str
    label: str
    configured: bool = False
    hint: str | None = None


class AiKeysResponse(BaseModel):
    keys: list[AiKeyState]


class AiKeyInput(BaseModel):
    # Bounded so a paste of the wrong thing cannot become a megabyte of
    # ciphertext in the database.
    key: str = Field(min_length=8, max_length=512)


class SummaryRequest(BaseModel):
    article_id: str = Field(min_length=1, max_length=128)
    # Leave null to let the server pick the reader's preferred stored key.
    provider: str | None = None
    # Ask for a fresh summary even though one is cached.
    refresh: bool = False


class SummaryResponse(BaseModel):
    provider: str
    label: str
    summary: Summary
    cached: bool = False


class ReadingEntry(BaseModel):
    """One story a reader has opened, for the continue-reading rail.

    The article travels with the entry because the cached feed page that first
    carried it may have been evicted long ago; a resume tile that renders an
    empty box would be worse than no rail at all.
    """

    article: Article | None = None
    article_id: str
    opened_at: datetime
    read: bool = False


class ReadingResponse(BaseModel):
    entries: list[ReadingEntry] = Field(default_factory=list)
    # Ids only, so a feed can dim what has already been read without carrying
    # the articles themselves. Bounded — see `Database.reading_read_ids`.
    read_ids: list[str] = Field(default_factory=list)


class ReadingPatch(BaseModel):
    """Marking a story read or unread. Opening one is the POST, not this."""

    # Strict on purpose: Pydantic's default lax mode quietly reads the strings
    # "yes", "no", "1" and "0" as booleans, so a loose client would be able to
    # send anything at all and still flip a reader's mark. The app itself sends
    # `{"read": true}` from `JSON.stringify`, so strictness costs nothing here.
    read: bool = Field(strict=True)


def as_detail(article: Article) -> ArticleDetail:
    """Widen an article to the reader-mode shape, preserving any body it has."""
    if isinstance(article, ArticleDetail):
        return article
    return ArticleDetail(**article.model_dump())
