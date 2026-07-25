"""Retention: what gets pruned, what is protected, and what stays deduplicated."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from vocast import library
from vocast.ingest.config import RetentionConfig
from vocast.ingest.db import Database, open_database
from vocast.ingest.models import EntryStatus, FeedEntry
from vocast.ingest.repository import EntryRepository, SourceRepository
from vocast.ingest.retention import Retention
from vocast.ingest.timeutils import to_iso, utcnow


@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "library"
    path.mkdir()
    monkeypatch.setattr(library, "LIBRARY_PATH", path)
    return path


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return open_database(tmp_path / "state.db")


@pytest.fixture
def entries(db: Database) -> EntryRepository:
    return EntryRepository(db)


@pytest.fixture
def source_id(db: Database) -> int:
    return (
        SourceRepository(db)
        .add(name="Example", kind="rss", url="https://example.com/feed.xml")
        .id
    )


def _episode(lib: Path, episode_id: str, *, age_days: float = 0, size: int = 1024):
    entry_dir = lib / episode_id
    entry_dir.mkdir(parents=True)
    (entry_dir / "audio.mp3").write_bytes(b"x" * size)
    (entry_dir / "meta.json").write_text(
        json.dumps(
            {
                "id": episode_id,
                "title": episode_id,
                "source": None,
                "synthesized_at": to_iso(utcnow() - timedelta(days=age_days)),
                "duration_seconds": 60.0,
                "voice": "af_heart",
                "engine": "kokoro",
            }
        )
    )
    return entry_dir


def _ingested(
    entries: EntryRepository, source_id: int, episode_id: str, guid: str | None = None
) -> int:
    entry = entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid=guid or episode_id,
            title=episode_id,
            article_url=f"https://example.com/{episode_id}",
            published_at=utcnow(),
        )
    )
    entries.mark_ready(entry.id, episode_id=episode_id)
    return entry.id


def _retention(entries: EntryRepository, lib: Path, **config) -> Retention:
    include_manual = config.pop("include_manual", False)
    return Retention(
        entries=entries,
        config=RetentionConfig(**config),
        library_path=lib,
        include_manual=include_manual,
    )


# --- disabled by default ---------------------------------------------------


def test_retention_does_nothing_when_disabled(
    lib: Path, entries: EntryRepository, source_id: int
):
    _episode(lib, "old", age_days=500)
    _ingested(entries, source_id, "old")

    report = _retention(entries, lib, enabled=False, max_age_days=1).apply()

    assert report.removed == []
    assert (lib / "old").exists()


# --- age limit -------------------------------------------------------------


def test_episode_older_than_the_age_limit_is_removed(
    lib: Path, entries: EntryRepository, source_id: int
):
    _episode(lib, "old", age_days=100)
    _ingested(entries, source_id, "old")

    report = _retention(
        entries, lib, enabled=True, max_age_days=90, max_episodes=None
    ).apply()

    assert report.removed == ["old"]
    assert not (lib / "old").exists()


def test_episode_within_the_age_limit_is_kept(
    lib: Path, entries: EntryRepository, source_id: int
):
    _episode(lib, "recent", age_days=10)
    _ingested(entries, source_id, "recent")

    report = _retention(
        entries, lib, enabled=True, max_age_days=90, max_episodes=None
    ).apply()

    assert report.removed == []
    assert (lib / "recent").exists()


# --- count limit -----------------------------------------------------------


def test_only_the_newest_episodes_are_kept(
    lib: Path, entries: EntryRepository, source_id: int
):
    for index in range(5):
        episode_id = f"2026060{index}T120000Z_ep_{index}"
        _episode(lib, episode_id, age_days=5 - index)
        _ingested(entries, source_id, episode_id)

    report = _retention(
        entries, lib, enabled=True, max_age_days=None, max_episodes=2
    ).apply()

    assert report.count == 3
    surviving = {e.id for e in library.list_entries()}
    assert surviving == {"20260604T120000Z_ep_4", "20260603T120000Z_ep_3"}


def test_count_limit_is_a_no_op_below_the_cap(
    lib: Path, entries: EntryRepository, source_id: int
):
    _episode(lib, "a")
    _ingested(entries, source_id, "a")

    report = _retention(
        entries, lib, enabled=True, max_age_days=None, max_episodes=10
    ).apply()

    assert report.removed == []


def test_an_episode_over_both_limits_is_only_counted_once(
    lib: Path, entries: EntryRepository, source_id: int
):
    _episode(lib, "old", age_days=500)
    _ingested(entries, source_id, "old")

    report = _retention(
        entries, lib, enabled=True, max_age_days=1, max_episodes=0
    ).apply()

    assert report.removed == ["old"]


# --- scope -----------------------------------------------------------------


def test_manually_added_episodes_are_protected_by_default(
    lib: Path, entries: EntryRepository
):
    """`vocast add` episodes cannot be regenerated from a feed, so they stay."""
    _episode(lib, "hand-added", age_days=500)

    report = _retention(entries, lib, enabled=True, max_age_days=1).apply()

    assert report.removed == []
    assert (lib / "hand-added").exists()


def test_manual_episodes_can_be_included_explicitly(
    lib: Path, entries: EntryRepository
):
    _episode(lib, "hand-added", age_days=500)

    report = _retention(
        entries, lib, enabled=True, max_age_days=1, include_manual=True
    ).apply()

    assert report.removed == ["hand-added"]


def test_pending_entries_are_untouched(
    lib: Path, entries: EntryRepository, source_id: int
):
    entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid="pending",
            title="Pending",
            article_url="https://example.com/pending",
            published_at=utcnow(),
        )
    )
    report = _retention(entries, lib, enabled=True, max_age_days=1).apply()
    assert report.removed == []
    assert entries.all()[0].status is EntryStatus.PENDING


# --- deduplication is preserved --------------------------------------------


def test_expired_episode_is_not_rediscovered_and_regenerated(
    lib: Path, entries: EntryRepository, source_id: int
):
    """The whole point: pruning must not start a delete/regenerate loop."""
    _episode(lib, "old", age_days=500)
    entry_id = _ingested(entries, source_id, "old", guid="stable-guid")

    _retention(entries, lib, enabled=True, max_age_days=1).apply()

    assert entries.get(entry_id).status is EntryStatus.EXPIRED
    reinsertion = entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid="stable-guid",
            title="old",
            article_url="https://example.com/old",
            published_at=utcnow(),
        )
    )
    assert reinsertion is None


def test_expired_episode_leaves_the_feed(
    lib: Path, entries: EntryRepository, source_id: int
):
    _episode(lib, "old", age_days=500)
    _ingested(entries, source_id, "old")

    _retention(entries, lib, enabled=True, max_age_days=1).apply()

    assert entries.published_episodes() == []


# --- safety ----------------------------------------------------------------


def test_dry_run_reports_without_deleting(
    lib: Path, entries: EntryRepository, source_id: int
):
    _episode(lib, "old", age_days=500)
    entry_id = _ingested(entries, source_id, "old")

    report = _retention(entries, lib, enabled=True, max_age_days=1).apply(dry_run=True)

    assert report.removed == ["old"]
    assert (lib / "old").exists()
    assert entries.get(entry_id).status is EntryStatus.READY


def test_freed_bytes_are_reported(lib: Path, entries: EntryRepository, source_id: int):
    _episode(lib, "old", age_days=500, size=4096)
    _ingested(entries, source_id, "old")

    report = _retention(entries, lib, enabled=True, max_age_days=1).apply()

    assert report.freed_bytes >= 4096


def test_deletion_outside_the_library_is_refused(
    lib: Path, entries: EntryRepository, source_id: int, tmp_path: Path
):
    """A crafted id must never let retention delete outside the library root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("important")

    _episode(lib, "old", age_days=500)
    _ingested(entries, source_id, "old")

    retention = Retention(
        entries=entries,
        config=RetentionConfig(enabled=True, max_age_days=1),
        library_path=tmp_path / "a-different-library",
    )
    report = retention.apply()

    assert report.removed == []
    assert report.refused
    assert (lib / "old").exists()
    assert (outside / "keep.txt").exists()
