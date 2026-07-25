"""Schema migrations, deduplication, atomic claiming, and retry bookkeeping."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from vocast.ingest.db import SCHEMA_VERSION, Database, open_database
from vocast.ingest.models import EntryStatus, FeedEntry
from vocast.ingest.repository import (
    DuplicateSourceError,
    EntryRepository,
    SourceRepository,
)
from vocast.ingest.timeutils import utcnow


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return open_database(tmp_path / "state.db")


@pytest.fixture
def sources(db: Database) -> SourceRepository:
    return SourceRepository(db)


@pytest.fixture
def entries(db: Database) -> EntryRepository:
    return EntryRepository(db)


def _feed_entry(source_id: int, guid: str, **overrides) -> FeedEntry:
    defaults = {
        "source_id": source_id,
        "external_guid": guid,
        "title": f"Article {guid}",
        "article_url": f"https://example.com/{guid}",
        "published_at": utcnow(),
    }
    defaults.update(overrides)
    return FeedEntry(**defaults)


def _add_source(sources: SourceRepository, name: str = "Example"):
    return sources.add(
        name=name, kind="rss", url=f"https://example.com/{name}/feed.xml"
    )


# --- migrations ------------------------------------------------------------


def test_migrate_sets_schema_version(db: Database):
    with db.reading() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_migrate_is_idempotent(tmp_path: Path):
    path = tmp_path / "state.db"
    open_database(path).close()
    reopened = open_database(path)
    assert reopened.migrate() == SCHEMA_VERSION


def test_migrate_preserves_existing_data(tmp_path: Path):
    path = tmp_path / "state.db"
    first = open_database(path)
    source = _add_source(SourceRepository(first))
    first.close()

    reopened = open_database(path)
    assert [s.id for s in SourceRepository(reopened).all()] == [source.id]


def test_migrate_refuses_newer_schema(tmp_path: Path):
    path = tmp_path / "state.db"
    open_database(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="only understands"):
        open_database(path)


# --- sources ---------------------------------------------------------------


def test_duplicate_source_url_is_rejected(sources: SourceRepository):
    sources.add(name="A", kind="rss", url="https://example.com/feed.xml")
    with pytest.raises(DuplicateSourceError):
        sources.add(name="B", kind="rss", url="https://example.com/feed.xml")


def test_upsert_updates_without_resetting_poll_history(sources: SourceRepository):
    source = sources.add(name="Old", kind="rss", url="https://example.com/feed.xml")
    sources.mark_success(source.id)

    updated = sources.upsert(
        name="New", kind="rss", url="https://example.com/feed.xml", enabled=False
    )

    assert updated.id == source.id
    assert updated.name == "New"
    assert updated.enabled is False
    assert updated.last_success_at is not None


def test_source_config_round_trips(sources: SourceRepository):
    source = sources.add(
        name="A",
        kind="rss",
        url="https://example.com/feed.xml",
        config={"headers": {"User-Agent": "vocast"}},
    )
    assert sources.get(source.id).config == {"headers": {"User-Agent": "vocast"}}


def test_removing_source_cascades_to_entries(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entries.insert_if_new(_feed_entry(source.id, "a"))

    assert sources.remove(source.id) is True
    assert entries.all() == []


def test_due_includes_never_checked_sources(sources: SourceRepository):
    source = _add_source(sources)
    assert [s.id for s in sources.due()] == [source.id]


def test_due_excludes_recently_checked_sources(sources: SourceRepository):
    source = _add_source(sources)
    sources.mark_success(source.id)
    assert sources.due() == []


def test_due_includes_sources_past_their_interval(sources: SourceRepository):
    source = sources.add(
        name="A",
        kind="rss",
        url="https://example.com/feed.xml",
        poll_interval_minutes=15,
    )
    sources.mark_success(source.id, now=utcnow() - timedelta(minutes=16))
    assert [s.id for s in sources.due()] == [source.id]


def test_due_excludes_disabled_sources(sources: SourceRepository):
    source = _add_source(sources)
    sources.set_enabled(source.id, False)
    assert sources.due() == []


def test_mark_error_records_message_and_keeps_source_due_later(
    sources: SourceRepository,
):
    source = _add_source(sources)
    sources.mark_error(source.id, "boom")

    stored = sources.get(source.id)
    assert stored.last_error == "boom"
    assert stored.last_success_at is None
    assert stored.last_checked_at is not None


def test_mark_success_clears_previous_error(sources: SourceRepository):
    source = _add_source(sources)
    sources.mark_error(source.id, "boom")
    sources.mark_success(source.id)
    assert sources.get(source.id).last_error is None


# --- entry deduplication ---------------------------------------------------


def test_insert_if_new_returns_entry_first_time(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    assert entry is not None
    assert entry.status is EntryStatus.PENDING


def test_insert_if_new_returns_none_for_duplicate_guid(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entries.insert_if_new(_feed_entry(source.id, "a"))
    assert entries.insert_if_new(_feed_entry(source.id, "a")) is None
    assert len(entries.all()) == 1


def test_same_guid_from_different_sources_is_not_a_duplicate(
    sources: SourceRepository, entries: EntryRepository
):
    first = _add_source(sources, name="One")
    second = _add_source(sources, name="Two")
    assert entries.insert_if_new(_feed_entry(first.id, "shared")) is not None
    assert entries.insert_if_new(_feed_entry(second.id, "shared")) is not None


# --- atomic claiming -------------------------------------------------------


def test_claim_next_returns_none_when_queue_empty(entries: EntryRepository):
    assert entries.claim_next() is None


def test_claim_next_marks_entry_processing(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entries.insert_if_new(_feed_entry(source.id, "a"))

    claimed = entries.claim_next()
    assert claimed.status is EntryStatus.PROCESSING
    assert claimed.claimed_at is not None


def test_claim_next_never_hands_out_the_same_entry_twice(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entries.insert_if_new(_feed_entry(source.id, "a"))

    assert entries.claim_next() is not None
    assert entries.claim_next() is None


def test_claim_next_is_fifo_by_discovery_order(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    first = entries.insert_if_new(_feed_entry(source.id, "first"))
    entries.insert_if_new(_feed_entry(source.id, "second"))

    assert entries.claim_next().id == first.id


def test_claim_next_skips_entries_waiting_on_backoff(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    entries.schedule_retry(
        entry.id, error="transient", next_retry_at=utcnow() + timedelta(minutes=5)
    )

    assert entries.claim_next() is None


def test_claim_next_picks_up_entry_once_backoff_elapses(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    due_at = utcnow() + timedelta(minutes=5)
    entries.schedule_retry(entry.id, error="transient", next_retry_at=due_at)

    claimed = entries.claim_next(now=due_at + timedelta(seconds=1))
    assert claimed.id == entry.id
    assert claimed.retry_count == 1


# --- state transitions -----------------------------------------------------


def test_mark_ready_records_episode_and_clears_error(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    entries.schedule_retry(entry.id, error="boom", next_retry_at=utcnow())
    entries.claim_next()

    entries.mark_ready(entry.id, episode_id="ep-1", content_hash="hash")

    stored = entries.get(entry.id)
    assert stored.status is EntryStatus.READY
    assert stored.vocast_episode_id == "ep-1"
    assert stored.content_hash == "hash"
    assert stored.error_message is None
    assert stored.claimed_at is None


def test_schedule_retry_returns_entry_to_pending_with_incremented_count(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    entries.claim_next()

    entries.schedule_retry(
        entry.id, error="timeout", next_retry_at=utcnow() + timedelta(minutes=5)
    )

    stored = entries.get(entry.id)
    assert stored.status is EntryStatus.PENDING
    assert stored.retry_count == 1
    assert stored.error_message == "timeout"
    assert stored.next_retry_at is not None


def test_mark_failed_leaves_entry_out_of_the_queue(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    entries.mark_failed(entry.id, error="permanent")

    assert entries.get(entry.id).status is EntryStatus.FAILED
    assert entries.claim_next() is None


def test_requeue_makes_failed_entry_claimable_again(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    entries.mark_failed(entry.id, error="permanent")

    assert entries.requeue(entry.id) is True

    claimed = entries.claim_next()
    assert claimed.id == entry.id
    assert claimed.retry_count == 0


def test_requeue_can_preserve_retry_count(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    entries.mark_failed(entry.id, error="permanent")

    entries.requeue(entry.id, reset_retries=False)
    assert entries.get(entry.id).retry_count == 1


def test_requeue_unknown_entry_reports_failure(entries: EntryRepository):
    assert entries.requeue(999) is False


# --- crash recovery --------------------------------------------------------


def test_reclaim_stale_returns_abandoned_entry_to_queue(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    entries.claim_next(now=utcnow() - timedelta(hours=2))

    assert entries.reclaim_stale(timeout=timedelta(minutes=60)) == 1
    assert entries.claim_next().id == entry.id


def test_reclaim_stale_leaves_fresh_claims_alone(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entries.insert_if_new(_feed_entry(source.id, "a"))
    entries.claim_next()

    assert entries.reclaim_stale(timeout=timedelta(minutes=60)) == 0
    assert entries.claim_next() is None


# --- reporting -------------------------------------------------------------


def test_counts_by_status_reports_every_state(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    ready = entries.insert_if_new(_feed_entry(source.id, "ready"))
    failed = entries.insert_if_new(_feed_entry(source.id, "failed"))
    entries.insert_if_new(_feed_entry(source.id, "pending"))
    entries.mark_ready(ready.id, episode_id="ep-1")
    entries.mark_failed(failed.id, error="nope")

    counts = entries.counts_by_status()
    assert counts["ready"] == 1
    assert counts["failed"] == 1
    assert counts["pending"] == 1
    assert counts["processing"] == 0


def test_published_episodes_are_newest_first(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    older = entries.insert_if_new(
        _feed_entry(source.id, "older", published_at=utcnow() - timedelta(days=2))
    )
    newer = entries.insert_if_new(
        _feed_entry(source.id, "newer", published_at=utcnow())
    )
    entries.mark_ready(older.id, episode_id="ep-older")
    entries.mark_ready(newer.id, episode_id="ep-newer")

    assert [e.episode_id for e in entries.published_episodes()] == [
        "ep-newer",
        "ep-older",
    ]


def test_published_episodes_can_be_filtered_by_source(
    sources: SourceRepository, entries: EntryRepository
):
    first = _add_source(sources, name="One")
    second = _add_source(sources, name="Two")
    a = entries.insert_if_new(_feed_entry(first.id, "a"))
    b = entries.insert_if_new(_feed_entry(second.id, "b"))
    entries.mark_ready(a.id, episode_id="ep-a")
    entries.mark_ready(b.id, episode_id="ep-b")

    episodes = entries.published_episodes(source_id=second.id)
    assert [e.episode_id for e in episodes] == ["ep-b"]
    assert episodes[0].source_name == "Two"


def test_published_episodes_excludes_unfinished_work(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entries.insert_if_new(_feed_entry(source.id, "pending"))
    assert entries.published_episodes() == []


def test_expire_keeps_dedup_row_so_article_is_not_regenerated(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    entries.mark_ready(entry.id, episode_id="ep-1")

    assert entries.expire(entry.id) is True
    assert entries.published_episodes() == []
    assert entries.insert_if_new(_feed_entry(source.id, "a")) is None
    assert entries.get(entry.id).status is EntryStatus.EXPIRED


# --- migration v1 -> v2 ----------------------------------------------------


def test_v1_database_is_upgraded_without_losing_data(tmp_path: Path):
    """An existing deployment must gain origin_name, not be recreated."""
    path = tmp_path / "state.db"
    from vocast.ingest.db import _SCHEMA_V1

    with sqlite3.connect(path) as raw:
        raw.executescript(_SCHEMA_V1)
        raw.execute("PRAGMA user_version=1")
        raw.execute(
            "INSERT INTO sources (name, kind, url, enabled, poll_interval_minutes,"
            " created_at, updated_at) VALUES ('Old','rss','https://e.com/f',1,15,'t','t')"
        )
        raw.execute(
            "INSERT INTO entries (source_id, external_guid, article_url, title,"
            " status, retry_count, created_at, updated_at)"
            " VALUES (1,'g','https://e.com/a','Kept','ready',0,'t','t')"
        )

    db = open_database(path)
    assert db.migrate() == SCHEMA_VERSION

    [entry] = EntryRepository(db).all()
    assert entry.title == "Kept"
    assert entry.origin_name is None


def test_origin_name_round_trips(sources: SourceRepository, entries: EntryRepository):
    source = _add_source(sources)
    entry = entries.insert_if_new(
        _feed_entry(source.id, "a", origin_name="Marginal Revolution")
    )
    assert entries.get(entry.id).origin_name == "Marginal Revolution"


def test_backfill_origin_labels_rows_recorded_without_one(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a"))
    assert entries.get(entry.id).origin_name is None

    updated = entries.backfill_origin(
        source.id, [_feed_entry(source.id, "a", origin_name="LessWrong")]
    )

    assert updated == 1
    assert entries.get(entry.id).origin_name == "LessWrong"


def test_backfill_origin_does_not_overwrite_an_existing_name(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    entry = entries.insert_if_new(_feed_entry(source.id, "a", origin_name="Original"))

    entries.backfill_origin(
        source.id, [_feed_entry(source.id, "a", origin_name="Different")]
    )

    assert entries.get(entry.id).origin_name == "Original"


def test_published_episodes_can_be_limited(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources)
    for index in range(5):
        entry = entries.insert_if_new(
            _feed_entry(
                source.id, f"g{index}", published_at=utcnow() - timedelta(days=index)
            )
        )
        entries.mark_ready(entry.id, episode_id=f"ep-{index}")

    limited = entries.published_episodes(limit=2)
    assert [e.episode_id for e in limited] == ["ep-0", "ep-1"]
