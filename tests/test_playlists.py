from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from vocast.ingest.db import _MIGRATIONS, SCHEMA_VERSION, Database, open_database
from vocast.ingest.models import FeedEntry
from vocast.ingest.repository import (
    EntryRepository,
    PlaylistRepository,
    SourceRepository,
)
from vocast.ingest.timeutils import utcnow


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return open_database(tmp_path / "state.db")


@pytest.fixture
def repositories(db: Database):
    sources = SourceRepository(db)
    entries = EntryRepository(db)
    playlists = PlaylistRepository(db)
    source = sources.add(name="Example", kind="rss", url="https://example.com/feed")
    return sources, entries, playlists, source


def _entry(entries: EntryRepository, source_id: int, guid: str):
    return entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid=guid,
            title=guid,
            article_url=f"https://example.com/{guid}",
            published_at=utcnow(),
        )
    )


def test_migration_creates_builtin_listen_later(db: Database):
    [playlist] = PlaylistRepository(db).all()

    assert playlist.slug == "listen-later"
    assert playlist.name == "Listen Later"
    assert playlist.is_system is True


def test_v7_migration_preserves_entries_and_is_idempotent(tmp_path: Path):
    path = tmp_path / "previous.db"
    with sqlite3.connect(path) as conn:
        for version, ddl in _MIGRATIONS:
            if version > 7:
                break
            conn.executescript(ddl)
        conn.execute("PRAGMA user_version=7")
        conn.execute(
            "INSERT INTO sources (name, kind, url, created_at, updated_at) "
            "VALUES ('Existing', 'rss', 'https://example.com/feed', 't', 't')"
        )
        conn.execute(
            "INSERT INTO entries (source_id, external_guid, article_url, title, status, "
            "created_at, updated_at) VALUES (1, 'kept', 'https://example.com/kept', "
            "'Kept', 'pending', 't', 't')"
        )

    migrated = open_database(path)

    assert migrated.migrate() == SCHEMA_VERSION
    assert migrated.migrate() == SCHEMA_VERSION
    assert EntryRepository(migrated).all()[0].title == "Kept"
    assert PlaylistRepository(migrated).get("listen-later") is not None


def test_add_is_idempotent_and_remove_missing_is_clear(repositories):
    _, entries, playlists, source = repositories
    entry = _entry(entries, source.id, "one")

    assert playlists.add_entry("listen-later", entry.id) is True
    assert playlists.add_entry("listen-later", entry.id) is False
    assert playlists.contains("listen-later", entry.id) is True
    assert playlists.remove_entry("listen-later", entry.id) is True
    assert playlists.remove_entry("listen-later", entry.id) is False


def test_entries_have_deterministic_playlist_order(repositories):
    _, entries, playlists, source = repositories
    now = utcnow()
    first = _entry(entries, source.id, "first")
    second = _entry(entries, source.id, "second")
    positioned = _entry(entries, source.id, "positioned")
    playlists.add_entry("listen-later", first.id, added_at=now)
    playlists.add_entry("listen-later", second.id, added_at=now + timedelta(minutes=1))
    playlists.add_entry("listen-later", positioned.id, position=1, added_at=now)

    assert [item.entry_id for item in playlists.entries("listen-later")] == [
        positioned.id,
        second.id,
        first.id,
    ]
    assert playlists.queued_entry_ids() == {first.id, second.id, positioned.id}


def test_deleting_entry_cascades_playlist_membership(repositories):
    sources, entries, playlists, source = repositories
    entry = _entry(entries, source.id, "one")
    playlists.add_entry("listen-later", entry.id)

    sources.remove(source.id)

    assert playlists.entries("listen-later") == []


def test_deleting_playlist_cascades_membership(repositories, db: Database):
    _, entries, playlists, source = repositories
    entry = _entry(entries, source.id, "one")
    playlists.add_entry("listen-later", entry.id)

    with db.transaction() as conn:
        conn.execute("DELETE FROM playlists WHERE slug = ?", ("listen-later",))

    with db.reading() as conn:
        count = conn.execute("SELECT COUNT(*) FROM playlist_entries").fetchone()[0]
    assert count == 0


# --- consumption retires an episode from every feed ------------------------


def _queued_ready_entry(db, repositories):
    _, entries, playlists, source = repositories
    entry = _entry(entries, source.id, "queued-one")
    entries.mark_ready(
        entry.id, episode_id="ep-queued", duration_seconds=60.0, audio_bytes=1000
    )
    playlists.add_entry("listen-later", entry.id)
    return entry


def _mark_downloaded(db, entry_id: int, when: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE entries SET read_at = ? WHERE id = ?", (when, entry_id))


def test_downloaded_episode_retires_from_the_playlist_feed_too(db, repositories):
    """An episode is consumed once, wherever it was fetched from.

    Downloading it via the combined feed must retire it from Listen Later as
    well, rather than leaving it queued in the feed it was not fetched through.
    """
    from vocast.ingest.feeds import collect_playlist_episodes

    _, _, playlists, _ = repositories
    entry = _queued_ready_entry(db, repositories)

    assert (
        len(
            collect_playlist_episodes(
                playlists, slug="listen-later", base_url="https://h"
            )
        )
        == 1
    )

    _mark_downloaded(db, entry.id, "2020-01-01T00:00:00+00:00")

    assert (
        collect_playlist_episodes(
            playlists,
            slug="listen-later",
            base_url="https://h",
            hide_read_before=utcnow() - timedelta(hours=48),
        )
        == []
    )


def test_a_recent_download_stays_in_the_playlist_feed(db, repositories):
    """The delay exists because downloading is not the same as listening."""
    from vocast.ingest.feeds import collect_playlist_episodes
    from vocast.ingest.timeutils import to_iso

    _, _, playlists, _ = repositories
    entry = _queued_ready_entry(db, repositories)
    _mark_downloaded(db, entry.id, to_iso(utcnow()))

    assert (
        len(
            collect_playlist_episodes(
                playlists,
                slug="listen-later",
                base_url="https://h",
                hide_read_before=utcnow() - timedelta(hours=48),
            )
        )
        == 1
    )


def test_playlist_feed_is_unfiltered_when_hiding_is_off(db, repositories):
    from vocast.ingest.feeds import collect_playlist_episodes

    _, _, playlists, _ = repositories
    entry = _queued_ready_entry(db, repositories)
    _mark_downloaded(db, entry.id, "2020-01-01T00:00:00+00:00")

    assert (
        len(
            collect_playlist_episodes(
                playlists, slug="listen-later", base_url="https://h"
            )
        )
        == 1
    )


def test_clearing_the_queue_removes_everything(db, repositories):
    _, entries, playlists, source = repositories
    for guid in ("a", "b", "c"):
        entry = _entry(entries, source.id, guid)
        playlists.add_entry("listen-later", entry.id)

    assert playlists.clear("listen-later") == 3
    assert playlists.entries("listen-later") == []
    assert playlists.queued_entry_ids() == set()


def test_clearing_an_empty_queue_is_harmless(db, repositories):
    _, _, playlists, _ = repositories
    assert playlists.clear("listen-later") == 0
