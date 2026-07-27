from __future__ import annotations

from pathlib import Path

import pytest

from vocast.ingest.db import open_database
from vocast.ingest.models import EntryStatus, FeedEntry
from vocast.ingest.repository import (
    ConsumptionRepository,
    EntryRepository,
    SourceRepository,
)
from vocast.ingest.timeutils import utcnow


@pytest.fixture
def repos(tmp_path: Path):
    db = open_database(tmp_path / "undownload.db")
    sources, entries = SourceRepository(db), EntryRepository(db)
    source = sources.add(name="FreshRSS", kind="freshrss_api", url="https://r.example")
    entry = entries.insert_if_new(
        FeedEntry(
            source_id=source.id,
            external_guid="guid-1",
            title="An article",
            article_url="https://example.com/a",
            published_at=utcnow(),
        )
    )
    entries.mark_ready(
        entry.id,
        episode_id="ep-1",
        content_hash="abc",
        duration_seconds=60.0,
        audio_bytes=1024,
    )
    return db, entries, ConsumptionRepository(db), entry


def test_undownload_puts_the_episode_back_in_the_feeds(repos):
    _, entries, consumption, entry = repos
    consumption.record_download("ep-1")
    assert entries.get(entry.id).read_at is not None

    consumption.set_read(entry.id, read=False)

    assert entries.get(entry.id).read_at is None


def test_undownload_clears_the_upstream_read_mark_too(repos):
    """Left set, the debounce would suppress marking the article read when it is
    deliberately downloaded again."""
    _, entries, consumption, entry = repos
    consumption.record_download("ep-1")
    consumption.mark_read_upstream(entry.id)

    consumption.set_read(entry.id, read=False)

    assert entries.get(entry.id).marked_read_at is None


def test_undownload_lifts_the_ignored_status(repos):
    """Read reconciliation ignores anything read upstream, so an accidental
    download usually leaves the entry ignored rather than merely downloaded."""
    _, entries, consumption, entry = repos
    consumption.record_download("ep-1")
    entries.set_status(entry.id, EntryStatus.IGNORED)

    consumption.set_read(entry.id, read=False)

    assert entries.get(entry.id).status is EntryStatus.READY


def test_undownload_leaves_an_unnarrated_entry_alone(repos):
    """Only an entry with audio can be restored to ready; a pending or failed
    one must keep its own status."""
    _, entries, consumption, _ = repos
    other = entries.insert_if_new(
        FeedEntry(
            source_id=1,
            external_guid="guid-2",
            title="Not narrated",
            article_url="https://example.com/b",
            published_at=utcnow(),
        )
    )
    entries.set_status(other.id, EntryStatus.IGNORED)

    consumption.set_read(other.id, read=False)

    assert entries.get(other.id).status is EntryStatus.IGNORED


def test_undownloading_an_unknown_entry_reports_nothing(repos):
    _, _, consumption, _ = repos

    assert consumption.set_read(9999, read=False) is None


class RecordingFetcher:
    """Captures the edit-tag call so the read/unread direction can be asserted."""

    def __init__(self) -> None:
        self.bodies: list[str] = []

    def __call__(self, url, **kwargs):
        from vocast.ingest.nethttp import Response

        if "ClientLogin" in url:
            return Response(url=url, status=200, body=b"Auth=token")
        if "token" in url:
            return Response(url=url, status=200, body=b"write-token")
        self.bodies.append((kwargs.get("data") or b"").decode())
        return Response(url=url, status=200, body=b"OK")


def _writer(fetcher):
    from vocast.ingest.freshrss_writer import FreshRSSWriter
    from vocast.ingest.models import Source

    source = Source(
        id=1,
        name="FreshRSS",
        kind="freshrss_api",
        url="https://reader.example",
        enabled=True,
        poll_interval_minutes=15,
        config={"username": "u", "api_password": "p"},
        last_checked_at=None,
        last_success_at=None,
        last_error=None,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    return FreshRSSWriter(source, fetcher=fetcher)


def test_marking_unread_removes_the_tag_rather_than_adding_it():
    """The endpoint is the same for both directions; only the parameter differs,
    and sending the wrong one would mark the article read a second time."""
    fetcher = RecordingFetcher()

    _writer(fetcher).mark_unread("guid-1")

    assert "r=user%2F-%2Fstate%2Fcom.google%2Fread" in fetcher.bodies[-1]
    assert "a=user" not in fetcher.bodies[-1]


def test_marking_read_still_adds_the_tag():
    fetcher = RecordingFetcher()

    _writer(fetcher).mark_read("guid-1")

    assert "a=user%2F-%2Fstate%2Fcom.google%2Fread" in fetcher.bodies[-1]


def test_marking_read_sets_the_marker(repos):
    _, entries, consumption, entry = repos

    consumption.set_read(entry.id, read=True)

    assert entries.get(entry.id).read_at is not None


def test_read_state_follows_the_reader_when_an_article_is_read_there(repos):
    """The reader is the authority: an article read there should leave the feed
    even though its audio was never fetched."""
    _, entries, _, entry = repos

    marked, cleared = entries.sync_read_upstream(entry.source_id, unread_guids=set())

    assert (marked, cleared) == (1, 0)
    assert entries.get(entry.id).read_at is not None


def test_read_state_follows_the_reader_back_to_unread(repos):
    """Marking an article unread in the reader brings the episode back."""
    _, entries, consumption, entry = repos
    consumption.set_read(entry.id, read=True)

    marked, cleared = entries.sync_read_upstream(
        entry.source_id, unread_guids={entry.external_guid}
    )

    assert (marked, cleared) == (0, 1)
    assert entries.get(entry.id).read_at is None


def test_syncing_leaves_entries_that_already_agree(repos):
    """No write, so updated_at is not churned on every poll."""
    _, entries, _, entry = repos

    assert entries.sync_read_upstream(
        entry.source_id, unread_guids={entry.external_guid}
    ) == (0, 0)


def test_syncing_covers_articles_that_were_never_narrated(repos):
    """read_at describes the article, not the episode. A comic that failed to
    narrate can still have been read, and showing it unread because no audio
    exists contradicts the reader."""
    _, entries, _, _ = repos
    failed = entries.insert_if_new(
        FeedEntry(
            source_id=1,
            external_guid="guid-comic",
            title="A comic",
            article_url="https://example.com/comic",
            published_at=utcnow(),
        )
    )
    entries.set_status(failed.id, EntryStatus.FAILED)

    entries.sync_read_upstream(1, unread_guids=set())

    assert entries.get(failed.id).read_at is not None
