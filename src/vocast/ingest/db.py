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

SCHEMA_VERSION = 2

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

# Ordered (version, ddl) pairs applied in sequence. Append new migrations;
# never edit a released one.
_MIGRATIONS: list[tuple[int, str]] = [(1, _SCHEMA_V1), (2, _SCHEMA_V2)]


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
