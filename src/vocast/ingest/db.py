"""SQLite connection handling and schema migrations.

A single connection guarded by a re-entrant lock serves the whole process.
The workload is tiny (one poller, a small number of workers, a low-traffic
feed server) so serializing access is cheaper than managing a pool, and it
keeps `:memory:` databases usable in tests. Transactions never wrap synthesis
work, so the lock is never held for long.

Cross-process safety still matters (`vocast poll` run by hand while `vocast
run` is up), which is why writes use WAL plus `BEGIN IMMEDIATE`.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 11

_SCHEMA_V1 = """
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 15,
    config_json TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(kind, url)
);

CREATE TABLE entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    external_guid TEXT NOT NULL,
    article_url TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    published_at TEXT,
    status TEXT NOT NULL,
    vocast_episode_id TEXT,
    content_hash TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    claimed_at TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE,
    UNIQUE(source_id, external_guid)
);

CREATE INDEX idx_entries_status ON entries(status, next_retry_at);
CREATE INDEX idx_entries_source ON entries(source_id);
CREATE INDEX idx_entries_episode ON entries(vocast_episode_id);
"""

# Records which upstream feed an article came from, so a combined feed can be
# labelled per publisher. Nullable: rows predating this are backfilled on the
# next poll, and not every source kind reports it.
_SCHEMA_V2 = """
ALTER TABLE entries ADD COLUMN origin_name TEXT;
"""

# Runtime state that must survive a restart, such as whether narration is
# paused. Kept in the database rather than a file so that a CLI invocation and
# the running service always agree.
_SCHEMA_V3 = """
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Ordered (version, ddl) pairs applied in sequence. Append new migrations;
# never edit a released one.
# Per-publication artwork, so an episode can carry its source's logo instead of
# whatever image happened to be in the article.
_SCHEMA_V4 = """
ALTER TABLE entries ADD COLUMN origin_image_url TEXT;
"""

# Duration and byte size of the generated audio. Recorded here so rendering a
# feed needs no filesystem access at all: reading one meta.json per episode is
# imperceptible for a handful and takes minutes for thousands on a network share.
_SCHEMA_V5 = """
ALTER TABLE entries ADD COLUMN duration_seconds REAL;
ALTER TABLE entries ADD COLUMN audio_bytes INTEGER;
"""

# When an episode's audio was fetched, and when the upstream article was marked
# read as a result. Downloading is the only signal a podcast client gives us --
# playback position is never reported back -- so it stands in for "consumed".
_SCHEMA_V6 = """
ALTER TABLE entries ADD COLUMN downloaded_at TEXT;
ALTER TABLE entries ADD COLUMN marked_read_at TEXT;
CREATE INDEX idx_entries_downloaded ON entries(downloaded_at);
"""

# The post's own text, when the article to narrate is the feed entry itself
# rather than whatever its link points at. Link-blog posts ("linked list" items)
# link outward to the thing being discussed, so following the link narrates
# someone else's article instead of the post.
_SCHEMA_V7 = """
ALTER TABLE entries ADD COLUMN feed_content TEXT;
"""

# Generic playlists keep the built-in queue extensible without adding user or
# sharing concepts. The seed is part of the migration so every database has the
# system playlist before repositories or routes can use it.
_SCHEMA_V8 = """
CREATE TABLE playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    is_system INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE playlist_entries (
    playlist_id INTEGER NOT NULL,
    entry_id INTEGER NOT NULL,
    position INTEGER,
    added_at TEXT NOT NULL,
    PRIMARY KEY (playlist_id, entry_id),
    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

CREATE INDEX idx_playlist_entries_order
ON playlist_entries(playlist_id, position, added_at);

INSERT INTO playlists (slug, name, is_system, created_at, updated_at)
VALUES (
    'listen-later', 'Listen Later', 1,
    -- Matches the ISO-8601 UTC form every other timestamp uses; SQLite's
    -- CURRENT_TIMESTAMP writes a space-separated, zoneless variant.
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
)
ON CONFLICT(slug) DO NOTHING;
"""

# How far synthesis has got, in chunks. Recorded so a long article shows
# progress rather than an opaque "processing": the longest here take hours.
_SCHEMA_V9 = """
ALTER TABLE entries ADD COLUMN progress_done INTEGER;
ALTER TABLE entries ADD COLUMN progress_total INTEGER;
"""

# A link post's own permalink. article_url holds the outbound link, which is
# what gets fetched when there is no body to narrate, so the permalink needs a
# column of its own rather than overwriting it.
_SCHEMA_V10 = """
ALTER TABLE entries ADD COLUMN post_url TEXT;
"""

# Renamed from downloaded_at. The column always meant "the listener has this",
# and it is kept in step with the reader's own read flag in both directions, so
# naming it after the transport that happened to set it was misleading.
_SCHEMA_V11 = """
ALTER TABLE entries RENAME COLUMN downloaded_at TO read_at;
"""

_MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
    (4, _SCHEMA_V4),
    (5, _SCHEMA_V5),
    (6, _SCHEMA_V6),
    (7, _SCHEMA_V7),
    (8, _SCHEMA_V8),
    (9, _SCHEMA_V9),
    (10, _SCHEMA_V10),
    (11, _SCHEMA_V11),
]


class Database:
    """Owns the SQLite connection and applies schema migrations."""

    def __init__(self, path: Path | str) -> None:
        self.path = path if path == ":memory:" else Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                if isinstance(self.path, Path):
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(self.path),
                    check_same_thread=False,
                    timeout=30.0,
                    isolation_level=None,
                )
                conn.row_factory = sqlite3.Row
                conn.create_function(
                    "unicode_casefold", 1, _unicode_casefold, deterministic=True
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=30000")
                self._conn = conn
            return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction, holding the process lock for its duration.

        `BEGIN IMMEDIATE` takes the SQLite write lock up front so concurrent
        processes fail fast into `busy_timeout` retries rather than deadlocking
        halfway through.
        """
        with self._lock:
            conn = self.connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self.connect()

    def migrate(self) -> int:
        """Bring the schema up to SCHEMA_VERSION; return the resulting version.

        Existing data is never dropped. A database created by a *newer* vocast
        is refused rather than downgraded.
        """
        with self._lock:
            conn = self.connect()
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database at {self.path} has schema version {current}, but this "
                    f"version of vocast only understands {SCHEMA_VERSION}. "
                    "Upgrade vocast or point --db at a different file."
                )
            for version, ddl in _MIGRATIONS:
                if version <= current:
                    continue
                # Transaction control lives inside the script: executescript
                # commits any pending transaction before it runs, so an outer
                # BEGIN would not cover the DDL and the user_version bump.
                conn.executescript(
                    f"BEGIN IMMEDIATE;\n{ddl}\nPRAGMA user_version={version};\nCOMMIT;"
                )
                current = version
            return current

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def open_database(path: Path | str) -> Database:
    """Open and migrate a database in one step."""
    db = Database(path)
    db.migrate()
    return db


def _unicode_casefold(value: object | None) -> str:
    return str(value).casefold() if value is not None else ""
