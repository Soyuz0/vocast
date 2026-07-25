"""Polling: insert-once semantics, fault isolation, and overlap prevention."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import ClassVar

import pytest

from vocast.ingest.db import Database, open_database
from vocast.ingest.models import FeedEntry, Source
from vocast.ingest.poller import Poller
from vocast.ingest.repository import EntryRepository, SourceRepository
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


class FakeAdapter:
    """Returns a scripted feed, or raises, without touching the network."""

    def __init__(self, source: Source, **_: object) -> None:
        self.source = source

    #: source_id -> list of (guid, url) tuples, or an Exception to raise
    scripts: ClassVar[dict[int, object]] = {}

    def fetch_entries(self) -> list[FeedEntry]:
        script = self.scripts.get(self.source.id, [])
        if isinstance(script, Exception):
            raise script
        return [
            FeedEntry(
                source_id=self.source.id,
                external_guid=guid,
                title=f"Article {guid}",
                article_url=url,
                published_at=utcnow(),
            )
            for guid, url in script
        ]


@pytest.fixture
def poller(sources: SourceRepository, entries: EntryRepository) -> Poller:
    FakeAdapter.scripts = {}
    return Poller(sources=sources, entries=entries, adapter_factory=FakeAdapter)


def _add_source(sources: SourceRepository, name: str, interval: int = 15) -> Source:
    return sources.add(
        name=name,
        kind="rss",
        url=f"https://example.com/{name}/feed.xml",
        poll_interval_minutes=interval,
    )


# --- discovery -------------------------------------------------------------


def test_first_poll_inserts_entries(
    poller: Poller, sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources, "one")
    FakeAdapter.scripts = {source.id: [("a", "https://example.com/a")]}

    report = poller.poll_due()

    assert report.inserted == 1
    assert [e.article_url for e in entries.all()] == ["https://example.com/a"]


def test_second_poll_of_unchanged_feed_inserts_nothing(
    poller: Poller, sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources, "one")
    FakeAdapter.scripts = {source.id: [("a", "https://example.com/a")]}

    poller.poll_all()
    second = poller.poll_all()

    assert second.inserted == 0
    assert len(entries.all()) == 1


def test_newly_published_article_is_inserted_on_a_later_poll(
    poller: Poller, sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources, "one")
    FakeAdapter.scripts = {source.id: [("a", "https://example.com/a")]}
    poller.poll_all()

    FakeAdapter.scripts = {
        source.id: [("b", "https://example.com/b"), ("a", "https://example.com/a")]
    }
    report = poller.poll_all()

    assert report.inserted == 1
    assert {e.external_guid for e in entries.all()} == {"a", "b"}


def test_poll_records_success_on_the_source(poller: Poller, sources: SourceRepository):
    source = _add_source(sources, "one")
    FakeAdapter.scripts = {source.id: []}

    poller.poll_all()

    stored = sources.get(source.id)
    assert stored.last_success_at is not None
    assert stored.last_error is None


# --- scheduling ------------------------------------------------------------


def test_poll_due_skips_sources_polled_recently(
    poller: Poller, sources: SourceRepository
):
    source = _add_source(sources, "one")
    FakeAdapter.scripts = {source.id: []}
    poller.poll_due()

    assert poller.poll_due().polled == 0


def test_poll_due_returns_to_a_source_after_its_interval(
    poller: Poller, sources: SourceRepository
):
    source = _add_source(sources, "one", interval=15)
    FakeAdapter.scripts = {source.id: []}
    poller.poll_due()

    later = utcnow() + timedelta(minutes=16)
    assert poller.poll_due(now=later).polled == 1


def test_poll_all_ignores_disabled_sources(
    poller: Poller, sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources, "one")
    FakeAdapter.scripts = {source.id: [("a", "https://example.com/a")]}
    sources.set_enabled(source.id, False)

    assert poller.poll_all().polled == 0
    assert entries.all() == []


# --- fault isolation -------------------------------------------------------


def test_one_broken_source_does_not_stop_the_others(
    poller: Poller, sources: SourceRepository, entries: EntryRepository
):
    broken = _add_source(sources, "broken")
    healthy = _add_source(sources, "healthy")
    FakeAdapter.scripts = {
        broken.id: RuntimeError("feed exploded"),
        healthy.id: [("a", "https://example.com/a")],
    }

    report = poller.poll_due()

    assert report.inserted == 1
    assert [f.source_name for f in report.failures] == ["broken"]
    assert len(entries.all()) == 1


def test_failure_is_recorded_on_the_source(poller: Poller, sources: SourceRepository):
    source = _add_source(sources, "broken")
    FakeAdapter.scripts = {source.id: RuntimeError("feed exploded")}

    poller.poll_due()

    stored = sources.get(source.id)
    assert "feed exploded" in stored.last_error
    assert stored.last_success_at is None
    assert stored.last_checked_at is not None


def test_unknown_source_kind_is_reported_not_raised(
    sources: SourceRepository, entries: EntryRepository
):
    source = sources.add(
        name="weird", kind="carrier_pigeon", url="https://example.com/f"
    )
    poller = Poller(sources=sources, entries=entries)

    report = poller.poll_source(source)

    assert not report.ok
    assert "carrier_pigeon" in report.error


def test_poll_does_not_generate_audio(
    poller: Poller, sources: SourceRepository, entries: EntryRepository
):
    """Discovery must only enqueue; synthesis is the worker's job."""
    source = _add_source(sources, "one")
    FakeAdapter.scripts = {source.id: [("a", "https://example.com/a")]}

    poller.poll_due()

    [entry] = entries.all()
    assert entry.status.value == "pending"
    assert entry.vocast_episode_id is None


# --- overlap prevention ----------------------------------------------------


def test_source_is_not_polled_twice_concurrently(
    sources: SourceRepository, entries: EntryRepository
):
    source = _add_source(sources, "one")
    reentered: list[bool] = []

    class ReentrantAdapter:
        def __init__(self, src: Source, **_: object) -> None:
            self.source = src

        def fetch_entries(self) -> list[FeedEntry]:
            # Re-enter the poller for the same source mid-fetch, exactly as an
            # overlapping scheduler tick would.
            reentered.append(poller.poll_source(self.source).skipped)
            return []

    poller = Poller(sources=sources, entries=entries, adapter_factory=ReentrantAdapter)
    poller.poll_source(source)

    assert reentered == [True]


def test_full_poll_disables_the_adapter_early_stop(
    sources: SourceRepository, entries: EntryRepository
):
    """Needed to re-read metadata for articles already recorded."""
    source = _add_source(sources, "one")
    seen_kwargs: list[object] = []

    class RecordingAdapter:
        def __init__(self, src: Source, **kwargs: object) -> None:
            self.source = src
            seen_kwargs.append(kwargs.get("known_guids"))

        def fetch_entries(self) -> list[FeedEntry]:
            return []

    poller = Poller(sources=sources, entries=entries, adapter_factory=RecordingAdapter)
    poller.poll_source(source)
    poller.poll_source(source, full=True)

    assert seen_kwargs[0] is not None, "routine poll should allow early-stop"
    assert seen_kwargs[1] is None, "full poll must disable early-stop"
