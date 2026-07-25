"""Data access for sources and entries.

Repositories own every SQL statement in the service; nothing outside this
module builds queries. All writes go through `Database.transaction()`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .db import Database
from .models import Entry, EntryStatus, FeedEntry, Source
from .timeutils import from_iso, to_iso, utcnow

_SOURCE_COLUMNS = """
    id, name, kind, url, enabled, poll_interval_minutes, config_json,
    last_checked_at, last_success_at, last_error, created_at, updated_at
"""

_ENTRY_COLUMNS = """
    id, source_id, external_guid, article_url, title, author, published_at,
    origin_name, status, vocast_episode_id, content_hash, retry_count,
    next_retry_at, claimed_at, error_message, created_at, updated_at
"""


class DuplicateSourceError(ValueError):
    """Raised when adding a source whose kind+url already exists."""


@dataclass(frozen=True)
class PublishedEpisode:
    """A generated episode joined to the source that discovered it."""

    episode_id: str
    entry_id: int
    source_id: int
    source_name: str
    article_url: str
    title: str
    author: str | None
    published_at: datetime | None
    origin_name: str | None = None
    summary: str | None = None


class SourceRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        *,
        name: str,
        kind: str,
        url: str,
        enabled: bool = True,
        poll_interval_minutes: int = 15,
        config: dict[str, Any] | None = None,
    ) -> Source:
        now = to_iso(utcnow())
        with self._db.transaction() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO sources (
                        name, kind, url, enabled, poll_interval_minutes,
                        config_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        kind,
                        url,
                        int(enabled),
                        poll_interval_minutes,
                        json.dumps(config) if config else None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateSourceError(
                    f"a {kind} source for {url} already exists"
                ) from exc
            row = conn.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return Source.from_row(row)

    def upsert(
        self,
        *,
        name: str,
        kind: str,
        url: str,
        enabled: bool = True,
        poll_interval_minutes: int = 15,
        config: dict[str, Any] | None = None,
    ) -> Source:
        """Create the source, or update its declarative fields if it exists.

        Used to reconcile the `sources:` block of the config file on startup.
        Poll bookkeeping (last_checked_at and friends) is deliberately left
        untouched so a restart does not look like a fresh source.
        """
        existing = self.find_by_url(kind=kind, url=url)
        if existing is None:
            return self.add(
                name=name,
                kind=kind,
                url=url,
                enabled=enabled,
                poll_interval_minutes=poll_interval_minutes,
                config=config,
            )
        now = to_iso(utcnow())
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE sources
                SET name = ?, enabled = ?, poll_interval_minutes = ?,
                    config_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    int(enabled),
                    poll_interval_minutes,
                    json.dumps(config) if config else None,
                    now,
                    existing.id,
                ),
            )
        refreshed = self.get(existing.id)
        assert refreshed is not None
        return refreshed

    def get(self, source_id: int) -> Source | None:
        with self._db.reading() as conn:
            row = conn.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        return Source.from_row(row) if row else None

    def find_by_url(self, *, kind: str, url: str) -> Source | None:
        with self._db.reading() as conn:
            row = conn.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE kind = ? AND url = ?",
                (kind, url),
            ).fetchone()
        return Source.from_row(row) if row else None

    def all(self, *, enabled_only: bool = False) -> list[Source]:
        sql = f"SELECT {_SOURCE_COLUMNS} FROM sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self._db.reading() as conn:
            rows = conn.execute(sql).fetchall()
        return [Source.from_row(r) for r in rows]

    def due(self, *, now: datetime | None = None) -> list[Source]:
        """Enabled sources that have never been checked or are past due."""
        moment = now or utcnow()
        return [s for s in self.all(enabled_only=True) if _is_due(s, moment)]

    def set_enabled(self, source_id: int, enabled: bool) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE sources SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), to_iso(utcnow()), source_id),
            )
        return cur.rowcount > 0

    def update(
        self,
        source_id: int,
        *,
        name: str | None = None,
        url: str | None = None,
        enabled: bool | None = None,
        poll_interval_minutes: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> Source | None:
        assignments: list[str] = []
        params: list[Any] = []
        if name is not None:
            assignments.append("name = ?")
            params.append(name)
        if url is not None:
            assignments.append("url = ?")
            params.append(url)
        if enabled is not None:
            assignments.append("enabled = ?")
            params.append(int(enabled))
        if poll_interval_minutes is not None:
            assignments.append("poll_interval_minutes = ?")
            params.append(poll_interval_minutes)
        if config is not None:
            assignments.append("config_json = ?")
            params.append(json.dumps(config) if config else None)
        if not assignments:
            return self.get(source_id)
        assignments.append("updated_at = ?")
        params.extend([to_iso(utcnow()), source_id])
        with self._db.transaction() as conn:
            try:
                conn.execute(
                    f"UPDATE sources SET {', '.join(assignments)} WHERE id = ?",
                    tuple(params),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateSourceError(
                    "another source already uses that kind and url"
                ) from exc
        return self.get(source_id)

    def remove(self, source_id: int) -> bool:
        """Delete a source and, by cascade, every entry it discovered."""
        with self._db.transaction() as conn:
            cur = conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return cur.rowcount > 0

    def mark_success(self, source_id: int, *, now: datetime | None = None) -> None:
        moment = to_iso(now or utcnow())
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE sources
                SET last_checked_at = ?, last_success_at = ?, last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (moment, moment, moment, source_id),
            )

    def mark_error(
        self, source_id: int, message: str, *, now: datetime | None = None
    ) -> None:
        moment = to_iso(now or utcnow())
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE sources
                SET last_checked_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (moment, message[:1000], moment, source_id),
            )

    def last_successful_poll(self) -> datetime | None:
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT MAX(last_success_at) AS latest FROM sources"
            ).fetchone()
        return from_iso(row["latest"]) if row else None


class EntryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert_if_new(self, entry: FeedEntry) -> Entry | None:
        """Insert a discovered article; return None if it was already known.

        The UNIQUE(source_id, external_guid) constraint is the authoritative
        deduplication guard, so concurrent pollers cannot both win.
        """
        now = to_iso(utcnow())
        with self._db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO entries (
                    source_id, external_guid, article_url, title, author,
                    published_at, origin_name, status, retry_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(source_id, external_guid) DO NOTHING
                """,
                (
                    entry.source_id,
                    entry.external_guid,
                    entry.article_url,
                    entry.title,
                    entry.author,
                    to_iso(entry.published_at),
                    entry.origin_name,
                    EntryStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return Entry.from_row(row)

    def get(self, entry_id: int) -> Entry | None:
        with self._db.reading() as conn:
            row = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return Entry.from_row(row) if row else None

    def all(
        self,
        *,
        status: EntryStatus | None = None,
        source_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Entry]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        with self._db.reading() as conn:
            rows = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM entries{where} "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [Entry.from_row(r) for r in rows]

    def claim_next(
        self, *, now: datetime | None = None, newest_first: bool = False
    ) -> Entry | None:
        """Atomically move one due pending entry to `processing` and return it.

        Selection and update happen inside one `BEGIN IMMEDIATE` transaction,
        so two workers can never claim the same entry.

        By default entries are claimed in discovery order, which is fair and
        predictable. `newest_first` instead takes the most recently *published*
        article, which is what you want when working through a large backlog:
        today's news gets narrated before a two-year-old post.
        """
        moment = now or utcnow()
        moment_iso = to_iso(moment)
        order = (
            "COALESCE(published_at, created_at) DESC, id DESC" if newest_first else "id"
        )
        with self._db.transaction() as conn:
            row = conn.execute(
                f"""
                SELECT id FROM entries
                WHERE status = ?
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY {order}
                LIMIT 1
                """,
                (EntryStatus.PENDING.value, moment_iso),
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                """
                UPDATE entries
                SET status = ?, claimed_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    EntryStatus.PROCESSING.value,
                    moment_iso,
                    moment_iso,
                    row["id"],
                    EntryStatus.PENDING.value,
                ),
            )
            if cur.rowcount == 0:
                return None
            claimed = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE id = ?", (row["id"],)
            ).fetchone()
        return Entry.from_row(claimed)

    def mark_ready(
        self, entry_id: int, *, episode_id: str, content_hash: str | None = None
    ) -> None:
        now = to_iso(utcnow())
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE entries
                SET status = ?, vocast_episode_id = ?, content_hash = ?,
                    error_message = NULL, next_retry_at = NULL, claimed_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (EntryStatus.READY.value, episode_id, content_hash, now, entry_id),
            )

    def schedule_retry(
        self, entry_id: int, *, error: str, next_retry_at: datetime
    ) -> None:
        """Return an entry to `pending` with an incremented attempt count."""
        now = to_iso(utcnow())
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE entries
                SET status = ?, retry_count = retry_count + 1, error_message = ?,
                    next_retry_at = ?, claimed_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    EntryStatus.PENDING.value,
                    error[:2000],
                    to_iso(next_retry_at),
                    now,
                    entry_id,
                ),
            )

    def mark_failed(self, entry_id: int, *, error: str) -> None:
        now = to_iso(utcnow())
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE entries
                SET status = ?, retry_count = retry_count + 1, error_message = ?,
                    next_retry_at = NULL, claimed_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (EntryStatus.FAILED.value, error[:2000], now, entry_id),
            )

    def requeue(
        self,
        entry_id: int,
        *,
        reset_retries: bool = True,
        clear_episode: bool = False,
    ) -> bool:
        """Put an entry back in the queue for immediate processing.

        clear_episode drops the pointer to the generated audio, for when that
        audio has been deleted and will be rebuilt.
        """
        now = to_iso(utcnow())
        retry_clause = "retry_count = 0," if reset_retries else ""
        episode_clause = "vocast_episode_id = NULL," if clear_episode else ""
        with self._db.transaction() as conn:
            cur = conn.execute(
                f"""
                UPDATE entries
                SET status = ?, {retry_clause} {episode_clause} next_retry_at = NULL,
                    claimed_at = NULL, error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (EntryStatus.PENDING.value, now, entry_id),
            )
        return cur.rowcount > 0

    def set_status(self, entry_id: int, status: EntryStatus) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE entries SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, to_iso(utcnow()), entry_id),
            )
        return cur.rowcount > 0

    def reclaim_stale(self, *, timeout: timedelta, now: datetime | None = None) -> int:
        """Return entries stuck in `processing` to `pending` after a crash.

        A worker that dies mid-synthesis leaves its claim behind; without this
        the article would never be retried.
        """
        moment = now or utcnow()
        cutoff = to_iso(moment - timeout)
        with self._db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE entries
                SET status = ?, claimed_at = NULL, updated_at = ?,
                    error_message = 'reclaimed after processing timeout'
                WHERE status = ?
                  AND (claimed_at IS NULL OR claimed_at <= ?)
                """,
                (
                    EntryStatus.PENDING.value,
                    to_iso(moment),
                    EntryStatus.PROCESSING.value,
                    cutoff,
                ),
            )
        return cur.rowcount

    def counts_by_status(self) -> dict[str, int]:
        with self._db.reading() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM entries GROUP BY status"
            ).fetchall()
        counts = {status.value: 0 for status in EntryStatus}
        counts.update({r["status"]: r["n"] for r in rows})
        return counts

    def published_episodes(
        self, *, source_id: int | None = None, limit: int | None = None
    ) -> list[PublishedEpisode]:
        """Ready episodes joined to their source, newest first.

        Ordering is fully deterministic: newest publication date first, ties
        broken by descending entry id, so the feed never reshuffles between
        requests.
        """
        clauses = ["e.status = ?", "e.vocast_episode_id IS NOT NULL"]
        params: list[Any] = [EntryStatus.READY.value]
        if source_id is not None:
            clauses.append("e.source_id = ?")
            params.append(source_id)
        with self._db.reading() as conn:
            rows = conn.execute(
                f"""
                SELECT e.vocast_episode_id, e.id AS entry_id, e.source_id,
                       s.name AS source_name, e.article_url, e.title, e.author,
                       e.published_at, e.origin_name
                FROM entries e
                JOIN sources s ON s.id = e.source_id
                WHERE {" AND ".join(clauses)}
                ORDER BY COALESCE(e.published_at, e.created_at) DESC, e.id DESC
                {"LIMIT ?" if limit else ""}
                """,
                tuple([*params, limit] if limit else params),
            ).fetchall()
        return [
            PublishedEpisode(
                episode_id=r["vocast_episode_id"],
                entry_id=r["entry_id"],
                source_id=r["source_id"],
                source_name=r["source_name"],
                article_url=r["article_url"],
                title=r["title"],
                author=r["author"],
                published_at=from_iso(r["published_at"]),
                origin_name=r["origin_name"],
            )
            for r in rows
        ]

    def backfill_origin(self, source_id: int, entries: Iterable[FeedEntry]) -> int:
        """Fill in origin_name for rows recorded before it was captured.

        Only touches rows where it is still NULL, so it is a no-op once the
        backlog has been labelled.
        """
        pairs = [
            (e.origin_name, source_id, e.external_guid)
            for e in entries
            if e.origin_name
        ]
        if not pairs:
            return 0
        with self._db.transaction() as conn:
            cur = conn.executemany(
                """
                UPDATE entries SET origin_name = ?
                WHERE source_id = ? AND external_guid = ? AND origin_name IS NULL
                """,
                pairs,
            )
            return cur.rowcount

    def known_guids(self, source_id: int, guids: Iterable[str]) -> set[str]:
        """Of these external guids, which does this source already track?

        Lets an adapter that pages through a large backlog stop as soon as it
        reaches articles already recorded, instead of re-downloading everything
        on every poll.
        """
        wanted = list(guids)
        if not wanted:
            return set()
        found: set[str] = set()
        with self._db.reading() as conn:
            # Chunked to stay well under SQLite's variable limit.
            for start in range(0, len(wanted), 500):
                batch = wanted[start : start + 500]
                placeholders = ",".join("?" * len(batch))
                rows = conn.execute(
                    f"SELECT external_guid FROM entries WHERE source_id = ? "
                    f"AND external_guid IN ({placeholders})",
                    (source_id, *batch),
                ).fetchall()
                found.update(r["external_guid"] for r in rows)
        return found

    def find_by_episode_id(self, episode_id: str) -> Entry | None:
        with self._db.reading() as conn:
            row = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE vocast_episode_id = ?",
                (episode_id,),
            ).fetchone()
        return Entry.from_row(row) if row else None

    def expire(self, entry_id: int) -> bool:
        """Mark a generated episode as removed while keeping the dedup row."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE entries
                SET status = ?, vocast_episode_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (EntryStatus.EXPIRED.value, to_iso(utcnow()), entry_id),
            )
        return cur.rowcount > 0


def _is_due(source: Source, now: datetime) -> bool:
    if source.last_checked_at is None:
        return True
    interval = timedelta(minutes=max(1, source.poll_interval_minutes))
    return source.last_checked_at + interval <= now
