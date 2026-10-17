"""SQLite persistence: response cache, quota counters, articles, accounts.

The free tiers are tight (NewsAPI allows 100 requests per day, total), so the
cache is not an optimisation here — it is what lets the feed keep working.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .schemas import (
    Article,
    ArticleDetail,
    Bookmark,
    ReadingEntry,
    StoryThread,
    Summary,
    User,
    as_detail,
)

# Bounded so a heavy reader's history cannot turn the resume rail or a feed's
# read-id set into an unbounded payload.
READING_LIMIT = 20
READING_READ_LIMIT = 500

# The archive is browsed a day at a time, so neither the index nor one day's
# listing should be able to grow without limit.
ARCHIVE_DAYS_LIMIT = 60
ARCHIVE_DAY_LIMIT = 100

# How many copies of one story a thread keeps. Well above what the clusterer
# ever merges, and a guard against a syndicated wire story arriving in bulk.
THREAD_COVERAGE_LIMIT = 24

SCHEMA = """
CREATE TABLE IF NOT EXISTS response_cache (
    cache_key  TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    provider TEXT NOT NULL,
    day      TEXT NOT NULL,
    calls    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, day)
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- Case-insensitive uniqueness without having to normalise on every write.
CREATE UNIQUE INDEX IF NOT EXISTS users_email ON users (lower(email));

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);

-- Keyed on (user_id, id): two readers saving the same story is normal, and the
-- old single-column key let one reader's save block another's.
CREATE TABLE IF NOT EXISTS bookmarks (
    user_id       TEXT NOT NULL,
    id            TEXT NOT NULL,
    payload       TEXT NOT NULL,
    bookmarked_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id)
);

CREATE INDEX IF NOT EXISTS bookmarks_bookmarked_at
    ON bookmarks (bookmarked_at DESC);

-- Every article the API has ever served, so the reader can look one up by id
-- after the feed page that carried it has been evicted. Bodies arrive later
-- than the summary (FreeNewsApi needs a second call per article) and are kept
-- once found: an article's text never changes, so this row has no expiry.
CREATE TABLE IF NOT EXISTS articles (
    id          TEXT PRIMARY KEY,
    source_uuid TEXT,
    payload     TEXT NOT NULL,
    seen_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS articles_seen_at ON articles (seen_at DESC);
CREATE INDEX IF NOT EXISTS articles_source_uuid ON articles (source_uuid);

-- Stories more than one outlet ran, kept past the life of the feed page that
-- produced them. Without this a thread would vanish with its cache entry and
-- a reader arriving from a bookmark would see a story that claims no one else
-- covered it.
CREATE TABLE IF NOT EXISTS threads (
    lead_id  TEXT PRIMARY KEY,
    payload  TEXT NOT NULL,
    seen_at  TEXT NOT NULL
);

-- Reverse index, because a reader can reach a story through a copy that is not
-- the lead — a search hit, a bookmark — and should still find the thread.
CREATE TABLE IF NOT EXISTS thread_members (
    article_id TEXT PRIMARY KEY,
    lead_id    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS thread_members_lead ON thread_members (lead_id);

-- Subjects and outlets a reader asked to see more of. A topic value is one of
-- the frontend's canonical category slugs, so it can be handed straight back
-- to the feed; a source value is an outlet's name as the providers print it.
CREATE TABLE IF NOT EXISTS follows (
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, value)
);

CREATE INDEX IF NOT EXISTS follows_user ON follows (user_id, created_at DESC);

-- One key per reader per provider. `ciphertext` is Fernet output, so a copy of
-- news.db on its own cannot be used to spend anyone's credits. `hint` is the
-- last four characters, kept in the clear so the account page can tell two
-- keys apart without ever holding the key itself.
CREATE TABLE IF NOT EXISTS provider_keys (
    user_id    TEXT NOT NULL,
    provider   TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    hint       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, provider)
);

-- Keyed on the article and the provider, not the reader: the text is identical
-- for everyone and temperature 0.2 makes a reply near-deterministic, so a
-- second reader with the same provider costs no upstream call.
CREATE TABLE IF NOT EXISTS summaries (
    article_id TEXT NOT NULL,
    provider   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (article_id, provider)
);

-- One row per story a reader has opened. Keyed on (user_id, article_id) so
-- re-opening a story bumps it to the top of the resume rail instead of adding
-- a second tile. The article travels with the row: the cached feed page that
-- first carried it may be evicted long before the rail is read.
CREATE TABLE IF NOT EXISTS reading (
    user_id    TEXT NOT NULL,
    article_id TEXT NOT NULL,
    payload    TEXT NOT NULL,
    opened_at  TEXT NOT NULL,
    read       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, article_id)
);

CREATE INDEX IF NOT EXISTS reading_opened_at ON reading (opened_at DESC);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _utcnow().date().isoformat()


class Database:
    """Small synchronous SQLite wrapper guarded by a re-entrant lock.

    The API is async but every statement here is a single-row read or write on
    a local file, so keeping it synchronous avoids a thread pool for no gain.
    """

    def __init__(self, path: Path, stale_ttl_seconds: int) -> None:
        self._stale_ttl = timedelta(seconds=stale_ttl_seconds)
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring a database written by an older build up to the current shape.

        `SCHEMA` uses `CREATE TABLE IF NOT EXISTS`, so an existing table keeps
        its original columns and never gains new ones.
        """
        self._migrate_bookmarks()
        self._migrate_article_dates()
        self._migrate_article_body_checks()

    def _migrate_bookmarks(self) -> None:
        """Rebuild `bookmarks` if it predates accounts.

        Rows are carried across unowned rather than deleted; they predate any
        account, so nothing can claim them.
        """
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(bookmarks)")}
        if not columns or "user_id" in columns:
            return

        self._conn.executescript(
            """
            ALTER TABLE bookmarks RENAME TO bookmarks_legacy;
            DROP INDEX IF EXISTS bookmarks_bookmarked_at;
            CREATE TABLE bookmarks (
                user_id       TEXT NOT NULL,
                id            TEXT NOT NULL,
                payload       TEXT NOT NULL,
                bookmarked_at TEXT NOT NULL,
                PRIMARY KEY (user_id, id)
            );
            CREATE INDEX IF NOT EXISTS bookmarks_bookmarked_at
                ON bookmarks (bookmarked_at DESC);
            INSERT INTO bookmarks (user_id, id, payload, bookmarked_at)
                SELECT '', id, payload, bookmarked_at FROM bookmarks_legacy;
            DROP TABLE bookmarks_legacy;
            """
        )

    def _migrate_article_dates(self) -> None:
        """Add `articles.published_at`, so the archive can be browsed by day.

        A publication date only exists inside the stored JSON, where SQL cannot
        reach it. The column is lifted out once — rows written before it existed
        are backfilled with `json_extract` — so an archive request sorts and
        groups on an index instead of parsing every payload.
        """
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(articles)")}
        if not columns:
            return

        if "published_at" not in columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN published_at TEXT")
            # `json_valid` guards the backfill: one malformed payload should not
            # be able to stop the application from starting.
            self._conn.execute(
                "UPDATE articles SET published_at = json_extract(payload, '$.published_at')"
                " WHERE json_valid(payload)"
            )

        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS articles_published_at ON articles (published_at DESC)"
        )

    def _migrate_article_body_checks(self) -> None:
        """Add `articles.body_checked_at`, when a publisher read was last tried.

        Reader mode reads a story's own page once. A page that yields nothing
        still has to be remembered as *tried*, or a paywalled site would be
        fetched again on every single view — which is exactly the bandwidth
        this column exists to save.
        """
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(articles)")}
        if columns and "body_checked_at" not in columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN body_checked_at TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- cache

    def cache_get(self, key: str) -> tuple[bytes, bool] | None:
        """Return `(payload, is_fresh)`, or None when there is nothing usable.

        A stale payload is still returned while it is younger than the stale
        window; callers decide whether to serve it or try the providers again.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, created_at, expires_at FROM response_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()

        if row is None:
            return None

        now = _utcnow()
        created_at = datetime.fromisoformat(row["created_at"])
        is_fresh = now < datetime.fromisoformat(row["expires_at"])
        still_usable = now - created_at < self._stale_ttl

        if not is_fresh and not still_usable:
            self.cache_delete(key)
            return None

        return row["payload"].encode("utf-8"), is_fresh

    def cache_set(self, key: str, payload: bytes, ttl_seconds: int) -> None:
        now = _utcnow()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO response_cache (cache_key, payload, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload = excluded.payload,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (key, payload.decode("utf-8"), now.isoformat(), expires_at.isoformat()),
            )
            self._conn.commit()

    def cache_delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM response_cache WHERE cache_key = ?", (key,))
            self._conn.commit()

    # ---------------------------------------------------------------- quota

    def usage_used(self, provider: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT calls FROM api_usage WHERE provider = ? AND day = ?",
                (provider, _today()),
            ).fetchone()
        return int(row["calls"]) if row else 0

    def usage_record(self, provider: str, calls: int = 1) -> int:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO api_usage (provider, day, calls) VALUES (?, ?, ?)
                ON CONFLICT(provider, day) DO UPDATE SET calls = calls + excluded.calls
                """,
                (provider, _today(), calls),
            )
            self._conn.commit()
        return self.usage_used(provider)

    # -------------------------------------------------------------- articles

    def articles_remember(self, articles: list[Article], *, uuid_of: dict[str, str]) -> None:
        """Record served articles, without discarding a body already known.

        A summary arriving from a feed must not overwrite the full text a
        previous detail call stored, so the existing row wins whenever it is
        the one holding a body.
        """
        if not articles:
            return

        with self._lock:
            known = self._payloads_for([article.id for article in articles])
            now = _utcnow().isoformat()

            for article in articles:
                incoming = as_detail(article)
                current = known.get(article.id)
                if current is not None and current.body and not incoming.body:
                    continue
                self._conn.execute(
                    """
                    INSERT INTO articles (id, source_uuid, payload, published_at, seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        payload = excluded.payload,
                        source_uuid = COALESCE(excluded.source_uuid, articles.source_uuid),
                        published_at = excluded.published_at,
                        seen_at = excluded.seen_at
                    """,
                    (
                        incoming.id,
                        uuid_of.get(incoming.id),
                        incoming.model_dump_json(),
                        incoming.published_at.isoformat(),
                        now,
                    ),
                )
            self._conn.commit()

    def article_get(self, article_id: str) -> ArticleDetail | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return ArticleDetail.model_validate_json(row["payload"])
        except ValueError:
            return None

    def articles_by_uuid(self, uuids: list[str]) -> dict[str, ArticleDetail]:
        """Which of these provider uuids already have a cached body."""
        if not uuids:
            return {}

        placeholders = ",".join("?" * len(uuids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT source_uuid, payload FROM articles WHERE source_uuid IN ({placeholders})",
                uuids,
            ).fetchall()

        found: dict[str, ArticleDetail] = {}
        for row in rows:
            try:
                found[row["source_uuid"]] = ArticleDetail.model_validate_json(row["payload"])
            except ValueError:
                continue
        return found

    def article_body_checked_at(self, article_id: str) -> datetime | None:
        """When a publisher read for this story was last attempted, if ever."""
        with self._lock:
            row = self._conn.execute(
                "SELECT body_checked_at FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
        if row is None or not row["body_checked_at"]:
            return None
        try:
            return datetime.fromisoformat(row["body_checked_at"])
        except ValueError:
            return None

    def article_record_body(self, article_id: str, body: str | None) -> ArticleDetail | None:
        """Attach recovered text to a stored story, or record that the try failed.

        A None `body` still stamps `body_checked_at`, because the cost worth
        avoiding is the repeat fetch, not the failed one. Returns the stored
        detail — updated when a body was given — or None if the row has gone.
        """
        now = _utcnow().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                detail = ArticleDetail.model_validate_json(row["payload"])
            except ValueError:
                return None

            if body:
                detail = detail.model_copy(update={"body": body})
                self._conn.execute(
                    "UPDATE articles SET payload = ?, body_checked_at = ? WHERE id = ?",
                    (detail.model_dump_json(), now, article_id),
                )
            else:
                self._conn.execute(
                    "UPDATE articles SET body_checked_at = ? WHERE id = ?", (now, article_id)
                )
            self._conn.commit()
        return detail

    def _payloads_for(self, ids: list[str]) -> dict[str, ArticleDetail]:
        if not ids:
            return {}

        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT payload FROM articles WHERE id IN ({placeholders})", ids
        ).fetchall()

        known: dict[str, ArticleDetail] = {}
        for row in rows:
            try:
                detail = ArticleDetail.model_validate_json(row["payload"])
            except ValueError:
                continue
            known[detail.id] = detail
        return known

    # -------------------------------------------------------------- threads

    def threads_remember(self, threads: list[StoryThread]) -> None:
        """Store each cluster whole, replacing whatever the last pass found."""
        if not threads:
            return

        now = _utcnow().isoformat()
        with self._lock:
            for thread in threads:
                coverage = thread.coverage[:THREAD_COVERAGE_LIMIT]
                stored = thread.model_copy(update={"coverage": coverage})
                self._conn.execute(
                    """
                    INSERT INTO threads (lead_id, payload, seen_at) VALUES (?, ?, ?)
                    ON CONFLICT(lead_id) DO UPDATE SET
                        payload = excluded.payload, seen_at = excluded.seen_at
                    """,
                    (stored.lead_id, stored.model_dump_json(), now),
                )
                for article in coverage:
                    self._conn.execute(
                        """
                        INSERT INTO thread_members (article_id, lead_id) VALUES (?, ?)
                        ON CONFLICT(article_id) DO UPDATE SET lead_id = excluded.lead_id
                        """,
                        (article.id, stored.lead_id),
                    )
            self._conn.commit()

    def thread_for(self, article_id: str) -> StoryThread | None:
        """The thread an article belongs to, whether it leads it or merely ran it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT threads.payload FROM thread_members"
                " JOIN threads ON threads.lead_id = thread_members.lead_id"
                " WHERE thread_members.article_id = ?",
                (article_id,),
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT payload FROM threads WHERE lead_id = ?", (article_id,)
                ).fetchone()

        if row is None:
            return None
        try:
            return StoryThread.model_validate_json(row["payload"])
        except ValueError:
            return None

    # -------------------------------------------------------------- archive

    def archive_days(self, limit: int = ARCHIVE_DAYS_LIMIT) -> list[tuple[str, int]]:
        """`(day, count)` for every day this store holds, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT date(published_at) AS day, COUNT(*) AS day_count FROM articles"
                " WHERE published_at IS NOT NULL"
                " GROUP BY day ORDER BY day DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(row["day"], int(row["day_count"])) for row in rows if row["day"]]

    def archive_articles(
        self, day: str, limit: int = ARCHIVE_DAY_LIMIT, offset: int = 0
    ) -> tuple[list[Article], int]:
        """One day's stories, newest first, with that day's total."""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS day_count FROM articles WHERE date(published_at) = ?",
                (day,),
            ).fetchone()
            rows = self._conn.execute(
                "SELECT payload FROM articles WHERE date(published_at) = ?"
                " ORDER BY published_at DESC LIMIT ? OFFSET ?",
                (day, limit, offset),
            ).fetchall()

        articles: list[Article] = []
        for row in rows:
            try:
                articles.append(Article.model_validate_json(row["payload"]))
            except ValueError:
                continue
        return articles, int(total["day_count"]) if total else 0

    # ------------------------------------------------------------- accounts

    def user_create(self, email: str, password_hash: str) -> User:
        """Insert a reader. Raises `sqlite3.IntegrityError` if the email is taken."""
        user = User(id=uuid.uuid4().hex, email=email, created_at=_utcnow())
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user.id, user.email, password_hash, user.created_at.isoformat()),
            )
            self._conn.commit()
        return user

    def credentials_by_email(self, email: str) -> tuple[User, str] | None:
        """The account and its stored hash, for a login attempt."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, email, password_hash, created_at FROM users WHERE lower(email) = lower(?)",
                (email,),
            ).fetchone()
        if row is None:
            return None
        return _user_from_row(row), row["password_hash"]

    def user_get(self, user_id: str) -> User | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, email, password_hash, created_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user_from_row(row) if row else None

    def session_create(self, token_hash: str, user_id: str, ttl_seconds: int) -> None:
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, user_id, now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat()),
            )
            self._conn.commit()

    def session_user(self, token_hash: str) -> User | None:
        """Resolve a cookie to a reader, evicting the row once it has expired."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT users.id, users.email, users.created_at, sessions.expires_at
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()

            if row is None:
                return None

            if _utcnow() >= datetime.fromisoformat(row["expires_at"]):
                self._conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                self._conn.commit()
                return None

        return _user_from_row(row)

    def session_delete(self, token_hash: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            self._conn.commit()

    # ------------------------------------------------------------ bookmarks

    def bookmarks_list(self, user_id: str) -> list[Bookmark]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload, bookmarked_at FROM bookmarks WHERE user_id = ? ORDER BY bookmarked_at DESC",
                (user_id,),
            ).fetchall()
        return [
            Bookmark.model_validate(
                {**json.loads(row["payload"]), "bookmarked_at": row["bookmarked_at"]}
            )
            for row in rows
        ]

    def bookmark_add(self, user_id: str, article: Article) -> Bookmark:
        bookmarked_at = _utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bookmarks (user_id, id, payload, bookmarked_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, id) DO NOTHING
                """,
                (user_id, article.id, article.model_dump_json(), bookmarked_at),
            )
            row = self._conn.execute(
                "SELECT payload, bookmarked_at FROM bookmarks WHERE user_id = ? AND id = ?",
                (user_id, article.id),
            ).fetchone()
            self._conn.commit()
        return Bookmark.model_validate(
            {**json.loads(row["payload"]), "bookmarked_at": row["bookmarked_at"]}
        )

    def bookmark_delete(self, user_id: str, article_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM bookmarks WHERE user_id = ? AND id = ?", (user_id, article_id)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    # -------------------------------------------------------------- reading

    def reading_record(self, user_id: str, article: Article) -> ReadingEntry:
        """Mark a story as opened now, without clearing an earlier read mark.

        Opening a story you have already finished should not make it look
        unread again — the rail's job is to say where you were, not to undo it.
        """
        now = _utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO reading (user_id, article_id, payload, opened_at, read)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(user_id, article_id) DO UPDATE SET
                    payload = excluded.payload,
                    opened_at = excluded.opened_at
                """,
                (user_id, article.id, article.model_dump_json(), now),
            )
            row = self._conn.execute(
                "SELECT article_id, payload, opened_at, read FROM reading"
                " WHERE user_id = ? AND article_id = ?",
                (user_id, article.id),
            ).fetchone()
            self._conn.commit()
        return _reading_from_row(row)

    def reading_list(self, user_id: str, limit: int = READING_LIMIT) -> list[ReadingEntry]:
        """The resume rail, most recently opened first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT article_id, payload, opened_at, read FROM reading"
                " WHERE user_id = ? ORDER BY opened_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [_reading_from_row(row) for row in rows]

    def reading_set_read(self, user_id: str, article_id: str, read: bool) -> ReadingEntry | None:
        """Flip a story's read mark, or None when it was never opened."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE reading SET read = ? WHERE user_id = ? AND article_id = ?",
                (1 if read else 0, user_id, article_id),
            )
            if cursor.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT article_id, payload, opened_at, read FROM reading"
                " WHERE user_id = ? AND article_id = ?",
                (user_id, article_id),
            ).fetchone()
            self._conn.commit()
        return _reading_from_row(row)

    def reading_read_ids(self, user_id: str, limit: int = READING_READ_LIMIT) -> list[str]:
        """Ids to dim in a feed, newest first and capped by `limit`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT article_id FROM reading WHERE user_id = ? AND read = 1"
                " ORDER BY opened_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [row["article_id"] for row in rows]

    # -------------------------------------------------------------- follows

    def follows_list(self, user_id: str) -> list[tuple[str, str]]:
        """`(kind, value)` newest first. Labels are resolved by the caller."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, value FROM follows WHERE user_id = ?"
                " ORDER BY created_at DESC, rowid DESC",
                (user_id,),
            ).fetchall()
        return [(row["kind"], row["value"]) for row in rows]

    def follow_add(self, user_id: str, kind: str, value: str) -> None:
        """Following something twice is not an error — the second is a no-op."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO follows (user_id, kind, value, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(user_id, kind, value) DO NOTHING",
                (user_id, kind, value, _utcnow().isoformat()),
            )
            self._conn.commit()

    def follow_delete(self, user_id: str, kind: str, value: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM follows WHERE user_id = ? AND kind = ? AND value = ?",
                (user_id, kind, value),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------- ai

    def provider_key_set(self, user_id: str, provider: str, ciphertext: str, hint: str) -> None:
        """Store or replace a reader's key for one provider."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO provider_keys (user_id, provider, ciphertext, hint, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, provider) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    hint = excluded.hint,
                    created_at = excluded.created_at
                """,
                (user_id, provider, ciphertext, hint, _utcnow().isoformat()),
            )
            self._conn.commit()

    def provider_key_get(self, user_id: str, provider: str) -> str | None:
        """The stored ciphertext, or None when this reader has no key for it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT ciphertext FROM provider_keys WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            ).fetchone()
        return row["ciphertext"] if row else None

    def provider_key_delete(self, user_id: str, provider: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM provider_keys WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def provider_hints(self, user_id: str) -> dict[str, str]:
        """`provider -> hint` for the account page, which must never see a key."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT provider, hint FROM provider_keys WHERE user_id = ?", (user_id,)
            ).fetchall()
        return {row["provider"]: row["hint"] for row in rows}

    def summary_get(self, article_id: str, provider: str) -> Summary | None:
        """A cached summary for this article, or None once it has aged out.

        Unlike the feed cache there is no stale-but-usable notion: a summary
        that has expired is simply written again on the next request.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, expires_at FROM summaries WHERE article_id = ? AND provider = ?",
                (article_id, provider),
            ).fetchone()

            if row is None:
                return None

            if _utcnow() >= datetime.fromisoformat(row["expires_at"]):
                self._conn.execute(
                    "DELETE FROM summaries WHERE article_id = ? AND provider = ?",
                    (article_id, provider),
                )
                self._conn.commit()
                return None

        try:
            return Summary.model_validate_json(row["payload"])
        except ValueError:
            return None

    def summary_set(
        self, article_id: str, provider: str, summary: Summary, ttl_seconds: int
    ) -> None:
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO summaries (article_id, provider, payload, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(article_id, provider) DO UPDATE SET
                    payload = excluded.payload,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    article_id,
                    provider,
                    summary.model_dump_json(),
                    now.isoformat(),
                    (now + timedelta(seconds=ttl_seconds)).isoformat(),
                ),
            )
            self._conn.commit()


def _user_from_row(row: sqlite3.Row) -> User:
    return User(id=row["id"], email=row["email"], created_at=datetime.fromisoformat(row["created_at"]))


def _reading_from_row(row: sqlite3.Row) -> ReadingEntry:
    """Rebuild a rail entry, tolerating a payload written by an older shape.

    A stored article that no longer validates is dropped rather than raised on:
    the tile still knows its id, so the reader can open it, and one bad row must
    not take down the whole rail.
    """
    try:
        article: Article | None = Article.model_validate_json(row["payload"])
    except ValueError:
        article = None
    return ReadingEntry(
        article=article,
        article_id=row["article_id"],
        opened_at=datetime.fromisoformat(row["opened_at"]),
        read=bool(row["read"]),
    )

