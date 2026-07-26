"""Search and filter ingestion entries for the web library."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .db import Database
from .models import EntryStatus, Source
from .repository import SourceRepository
from .timeutils import from_iso, to_iso

_ORIGIN_EXPRESSION = "COALESCE(NULLIF(TRIM(e.origin_name), ''), s.name)"
_SORTS = {
    "published_desc": "COALESCE(e.published_at, e.created_at) DESC, e.id DESC",
    "published_asc": "COALESCE(e.published_at, e.created_at) ASC, e.id ASC",
    "title_asc": "e.title COLLATE NOCASE ASC, e.id ASC",
    "title_desc": "e.title COLLATE NOCASE DESC, e.id DESC",
    "duration_asc": "e.duration_seconds IS NULL, e.duration_seconds ASC, e.id ASC",
    "duration_desc": "e.duration_seconds IS NULL, e.duration_seconds DESC, e.id DESC",
}


@dataclass(frozen=True)
class LibraryQuery:
    search: str | None = None
    source_id: int | None = None
    origin_id: str | None = None
    status: EntryStatus | None = None
    queued: bool | None = None
    downloaded: bool | None = None
    published_after: datetime | None = None
    published_before: datetime | None = None
    min_duration_seconds: int | None = None
    max_duration_seconds: int | None = None
    sort: str = "published_desc"
    page: int = 1
    page_size: int = 50


@dataclass(frozen=True)
class LibraryItem:
    entry_id: int
    episode_id: str | None
    title: str
    author: str | None
    article_url: str
    source_id: int
    source_name: str
    origin_id: str
    origin_name: str
    published_at: datetime | None
    duration_seconds: float | None
    audio_bytes: int | None
    status: EntryStatus
    downloaded_at: datetime | None
    queued: bool


@dataclass(frozen=True)
class LibraryPage:
    items: list[LibraryItem]
    total: int
    query: LibraryQuery

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.query.page_size - 1) // self.query.page_size)


@dataclass(frozen=True)
class LibraryOrigin:
    id: str
    name: str


class LibraryQueryService:
    def __init__(
        self, db: Database, *, default_page_size: int = 50, max_page_size: int = 100
    ) -> None:
        if default_page_size < 1 or max_page_size < 1:
            raise ValueError("page sizes must be positive")
        self._db = db
        self._default_page_size = min(default_page_size, max_page_size)
        self._max_page_size = max_page_size

    def search(self, query: LibraryQuery) -> LibraryPage:
        normalized = self._normalize(query)
        clauses, params = self._filters(normalized)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        playlist_join = """
            LEFT JOIN playlist_entries pe
              ON pe.entry_id = e.id
             AND pe.playlist_id = (
                 SELECT id FROM playlists WHERE slug = 'listen-later'
             )
        """
        with self._db.reading() as conn:
            total = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM entries e
                JOIN sources s ON s.id = e.source_id
                {playlist_join}
                {where}
                """,
                tuple(params),
            ).fetchone()[0]
            last_page = max(
                1, (total + normalized.page_size - 1) // normalized.page_size
            )
            normalized = replace(normalized, page=min(normalized.page, last_page))
            rows = conn.execute(
                f"""
                SELECT e.id, e.vocast_episode_id, e.title, e.author, e.article_url,
                       e.source_id, s.name AS source_name,
                       {_ORIGIN_EXPRESSION} AS origin_name,
                       UNICODE_CASEFOLD(TRIM({_ORIGIN_EXPRESSION})) AS origin_id,
                       e.published_at, e.duration_seconds, e.audio_bytes, e.status,
                       e.downloaded_at, pe.entry_id IS NOT NULL AS queued
                FROM entries e
                JOIN sources s ON s.id = e.source_id
                {playlist_join}
                {where}
                ORDER BY {_SORTS[normalized.sort]}
                LIMIT ? OFFSET ?
                """,
                (
                    *params,
                    normalized.page_size,
                    (normalized.page - 1) * normalized.page_size,
                ),
            ).fetchall()
        return LibraryPage(
            items=[self._item_from_row(row) for row in rows],
            total=total,
            query=normalized,
        )

    def sources(self) -> list[Source]:
        return SourceRepository(self._db).all()

    def origins(self) -> list[LibraryOrigin]:
        with self._db.reading() as conn:
            rows = conn.execute(
                f"""
                SELECT UNICODE_CASEFOLD(TRIM({_ORIGIN_EXPRESSION})) AS origin_id,
                       MIN({_ORIGIN_EXPRESSION}) AS origin_name
                FROM entries e JOIN sources s ON s.id = e.source_id
                WHERE {_ORIGIN_EXPRESSION} != ''
                GROUP BY UNICODE_CASEFOLD(TRIM({_ORIGIN_EXPRESSION}))
                ORDER BY origin_name COLLATE NOCASE
                """
            ).fetchall()
        return [
            LibraryOrigin(id=row["origin_id"], name=row["origin_name"]) for row in rows
        ]

    def _normalize(self, query: LibraryQuery) -> LibraryQuery:
        search = query.search.strip() if query.search else None
        origin_id = query.origin_id.strip() if query.origin_id else None
        return replace(
            query,
            search=search or None,
            origin_id=origin_id.casefold() if origin_id else None,
            sort=query.sort if query.sort in _SORTS else "published_desc",
            page=max(1, query.page),
            page_size=(
                self._default_page_size
                if query.page_size < 1
                else min(query.page_size, self._max_page_size)
            ),
        )

    def _filters(self, query: LibraryQuery) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.search:
            pattern = f"%{_escape_like(query.search.casefold())}%"
            clauses.append(
                "(UNICODE_CASEFOLD(e.title) LIKE ? ESCAPE '\\' "
                f"OR UNICODE_CASEFOLD({_ORIGIN_EXPRESSION}) LIKE ? ESCAPE '\\' "
                "OR UNICODE_CASEFOLD(s.name) LIKE ? ESCAPE '\\' "
                "OR UNICODE_CASEFOLD(COALESCE(e.author, '')) LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern] * 4)
        if query.source_id is not None:
            clauses.append("e.source_id = ?")
            params.append(query.source_id)
        if query.origin_id:
            clauses.append(f"UNICODE_CASEFOLD(TRIM({_ORIGIN_EXPRESSION})) = ?")
            params.append(query.origin_id)
        if query.status is not None:
            clauses.append("e.status = ?")
            params.append(query.status.value)
        if query.queued is not None:
            clauses.append(
                "pe.entry_id IS NOT NULL" if query.queued else "pe.entry_id IS NULL"
            )
        if query.downloaded is not None:
            clauses.append(
                "e.downloaded_at IS NOT NULL"
                if query.downloaded
                else "e.downloaded_at IS NULL"
            )
        if query.published_after is not None:
            clauses.append("e.published_at >= ?")
            params.append(to_iso(query.published_after))
        if query.published_before is not None:
            clauses.append("e.published_at <= ?")
            params.append(to_iso(query.published_before))
        if query.min_duration_seconds is not None:
            clauses.append("e.duration_seconds >= ?")
            params.append(max(0, query.min_duration_seconds))
        if query.max_duration_seconds is not None:
            clauses.append("e.duration_seconds <= ?")
            params.append(max(0, query.max_duration_seconds))
        return clauses, params

    @staticmethod
    def _item_from_row(row) -> LibraryItem:
        origin_name = row["origin_name"]
        return LibraryItem(
            entry_id=row["id"],
            episode_id=row["vocast_episode_id"],
            title=row["title"],
            author=row["author"],
            article_url=row["article_url"],
            source_id=row["source_id"],
            source_name=row["source_name"],
            origin_id=row["origin_id"],
            origin_name=origin_name,
            published_at=from_iso(row["published_at"]),
            duration_seconds=row["duration_seconds"],
            audio_bytes=row["audio_bytes"],
            status=EntryStatus(row["status"]),
            downloaded_at=from_iso(row["downloaded_at"]),
            queued=bool(row["queued"]),
        )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
