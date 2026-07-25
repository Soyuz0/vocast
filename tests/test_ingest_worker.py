"""Worker behavior: success, retry, permanent failure, and crash recovery.

The generator is stubbed throughout: no network, no model download, no speech.
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

import pytest

from vocast.ingest.config import WorkerConfig
from vocast.ingest.db import Database, open_database
from vocast.ingest.generator import (
    GeneratedEpisode,
    PermanentGenerationError,
    TransientGenerationError,
)
from vocast.ingest.models import EntryStatus, FeedEntry
from vocast.ingest.repository import EntryRepository, SourceRepository
from vocast.ingest.timeutils import utcnow
from vocast.ingest.worker import Worker


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


class StubGenerator:
    """Returns a canned episode, or raises a scripted sequence of errors."""

    def __init__(self, *, results: list[object] | None = None) -> None:
        self.results = results or []
        self.calls: list[tuple[str, str | None]] = []
        self.bylines: list[str | None] = []
        self.covers: list[str | None] = []
        self.replaced: list[str | None] = []
        self.bodies: list[str | None] = []

    def generate_from_url(
        self,
        url: str,
        *,
        title: str | None = None,
        byline: str | None = None,
        cover_url: str | None = None,
        replace_episode_id: str | None = None,
        content_html: str | None = None,
    ) -> GeneratedEpisode:
        self.calls.append((url, title))
        self.bylines.append(byline)
        self.covers.append(cover_url)
        self.replaced.append(replace_episode_id)
        self.bodies.append(content_html)
        result = (
            self.results.pop(0)
            if self.results
            else GeneratedEpisode(
                episode_id=f"ep-{len(self.calls)}",
                title=title or "untitled",
                audio_path="/tmp/audio.mp3",
                duration_seconds=61.5,
                content_hash="deadbeef",
            )
        )
        if isinstance(result, Exception):
            raise result
        return result


def _queue(entries: EntryRepository, source_id: int, guid: str = "a"):
    return entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid=guid,
            title=f"Article {guid}",
            article_url=f"https://example.com/{guid}",
            published_at=utcnow(),
        )
    )


def _worker(entries: EntryRepository, generator, **config) -> Worker:
    return Worker(entries=entries, generator=generator, config=WorkerConfig(**config))


# --- success ---------------------------------------------------------------


def test_successful_generation_marks_entry_ready(
    entries: EntryRepository, source_id: int
):
    entry = _queue(entries, source_id)
    worker = _worker(entries, StubGenerator())

    outcome = worker.process_next()

    assert outcome.ok
    stored = entries.get(entry.id)
    assert stored.status is EntryStatus.READY
    assert stored.vocast_episode_id == "ep-1"
    assert stored.content_hash == "deadbeef"


def test_worker_passes_the_article_url_and_title_to_the_pipeline(
    entries: EntryRepository, source_id: int
):
    _queue(entries, source_id, "post")
    generator = StubGenerator()

    _worker(entries, generator).process_next()

    assert generator.calls == [("https://example.com/post", "Article post")]


def test_process_next_returns_none_on_an_empty_queue(entries: EntryRepository):
    assert _worker(entries, StubGenerator()).process_next() is None


def test_drain_processes_every_queued_entry(entries: EntryRepository, source_id: int):
    for guid in ("a", "b", "c"):
        _queue(entries, source_id, guid)

    outcomes = _worker(entries, StubGenerator()).drain()

    assert len(outcomes) == 3
    assert all(o.ok for o in outcomes)


def test_drain_respects_max_entries(entries: EntryRepository, source_id: int):
    for guid in ("a", "b", "c"):
        _queue(entries, source_id, guid)

    assert len(_worker(entries, StubGenerator()).drain(max_entries=2)) == 2


def test_entry_is_not_generated_twice(entries: EntryRepository, source_id: int):
    _queue(entries, source_id)
    generator = StubGenerator()
    worker = _worker(entries, generator)

    worker.drain()
    worker.drain()

    assert len(generator.calls) == 1


# --- transient failure -----------------------------------------------------


def test_transient_failure_schedules_a_retry(entries: EntryRepository, source_id: int):
    entry = _queue(entries, source_id)
    generator = StubGenerator(results=[TransientGenerationError("timeout")])

    outcome = _worker(entries, generator).process_next()

    assert outcome.retrying
    stored = entries.get(entry.id)
    assert stored.status is EntryStatus.PENDING
    assert stored.retry_count == 1
    assert stored.next_retry_at is not None


def test_retry_succeeds_on_the_second_attempt(entries: EntryRepository, source_id: int):
    entry = _queue(entries, source_id)
    generator = StubGenerator(results=[TransientGenerationError("blip")])
    worker = _worker(entries, generator, base_retry_minutes=5)

    worker.process_next()
    # Jump past the backoff window rather than sleeping.
    outcome = worker.process_next(now=utcnow() + timedelta(minutes=6))

    assert outcome.ok
    assert entries.get(entry.id).status is EntryStatus.READY


def test_backed_off_entry_is_not_retried_immediately(
    entries: EntryRepository, source_id: int
):
    _queue(entries, source_id)
    generator = StubGenerator(results=[TransientGenerationError("blip")])
    worker = _worker(entries, generator)

    worker.process_next()

    assert worker.process_next() is None
    assert len(generator.calls) == 1


def test_unknown_error_is_treated_as_transient(
    entries: EntryRepository, source_id: int
):
    entry = _queue(entries, source_id)
    generator = StubGenerator(results=[RuntimeError("something odd")])

    outcome = _worker(entries, generator).process_next()

    assert outcome.retrying
    assert entries.get(entry.id).status is EntryStatus.PENDING


def test_retries_are_exhausted_into_failed(entries: EntryRepository, source_id: int):
    entry = _queue(entries, source_id)
    generator = StubGenerator(
        results=[TransientGenerationError("down") for _ in range(5)]
    )
    worker = _worker(entries, generator, max_retries=5, base_retry_minutes=1)

    moment = utcnow()
    for _ in range(5):
        worker.process_next(now=moment)
        moment += timedelta(hours=12)

    stored = entries.get(entry.id)
    assert stored.status is EntryStatus.FAILED
    assert stored.retry_count == 5
    assert len(generator.calls) == 5


# --- backoff policy --------------------------------------------------------


def test_backoff_grows_exponentially():
    worker = _worker(None, None, base_retry_minutes=5, max_retry_minutes=360)
    delays = [worker.retry_delay(n).total_seconds() / 60 for n in range(1, 5)]
    assert delays == [5, 10, 20, 40]


def test_backoff_is_capped():
    worker = _worker(None, None, base_retry_minutes=5, max_retry_minutes=360)
    assert worker.retry_delay(20).total_seconds() / 60 == 360


# --- permanent failure -----------------------------------------------------


def test_permanent_failure_skips_retries(entries: EntryRepository, source_id: int):
    entry = _queue(entries, source_id)
    generator = StubGenerator(results=[PermanentGenerationError("404 gone")])

    outcome = _worker(entries, generator).process_next()

    assert not outcome.ok
    assert not outcome.retrying
    stored = entries.get(entry.id)
    assert stored.status is EntryStatus.FAILED
    assert "404 gone" in stored.error_message


def test_failed_entry_can_be_requeued_and_then_succeeds(
    entries: EntryRepository, source_id: int
):
    entry = _queue(entries, source_id)
    generator = StubGenerator(results=[PermanentGenerationError("paywall")])
    worker = _worker(entries, generator)
    worker.process_next()

    entries.requeue(entry.id)
    outcome = worker.process_next()

    assert outcome.ok
    assert entries.get(entry.id).status is EntryStatus.READY


# --- crash recovery --------------------------------------------------------


def test_stale_processing_entry_is_reclaimed_and_regenerated(
    entries: EntryRepository, source_id: int
):
    entry = _queue(entries, source_id)
    # Simulate a worker that claimed the entry and then died.
    entries.claim_next(now=utcnow() - timedelta(hours=3))
    worker = _worker(entries, StubGenerator(), processing_timeout_minutes=60)

    assert worker.reclaim_stale() == 1
    assert worker.process_next().ok
    assert entries.get(entry.id).status is EntryStatus.READY


def test_reclaim_leaves_a_live_claim_alone(entries: EntryRepository, source_id: int):
    _queue(entries, source_id)
    entries.claim_next()
    worker = _worker(entries, StubGenerator(), processing_timeout_minutes=60)

    assert worker.reclaim_stale() == 0
    assert worker.process_next() is None


def test_two_workers_never_generate_the_same_entry(
    entries: EntryRepository, source_id: int
):
    _queue(entries, source_id)
    first, second = StubGenerator(), StubGenerator()

    _worker(entries, first).process_next()
    _worker(entries, second).process_next()

    assert len(first.calls) + len(second.calls) == 1


# --- claim ordering --------------------------------------------------------


def test_newest_published_article_is_claimed_first(
    entries: EntryRepository, source_id: int
):
    """Working a large backlog should narrate today's news before old posts."""
    old = entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid="old",
            title="Two Years Ago",
            article_url="https://example.com/old",
            published_at=utcnow() - timedelta(days=730),
        )
    )
    new = entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid="new",
            title="Today",
            article_url="https://example.com/new",
            published_at=utcnow(),
        )
    )
    assert old.id < new.id  # discovered first, so FIFO would pick it

    worker = _worker(entries, StubGenerator(), newest_first=True)
    assert worker.process_next().entry_id == new.id
    assert worker.process_next().entry_id == old.id


def test_discovery_order_is_the_default(entries: EntryRepository, source_id: int):
    old = entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid="old",
            title="Old",
            article_url="https://example.com/old",
            published_at=utcnow() - timedelta(days=730),
        )
    )
    entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid="new",
            title="New",
            article_url="https://example.com/new",
            published_at=utcnow(),
        )
    )

    assert _worker(entries, StubGenerator()).process_next().entry_id == old.id


def test_newest_first_falls_back_to_discovery_time_without_a_date(
    entries: EntryRepository, source_id: int
):
    undated = entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid="undated",
            title="No Date",
            article_url="https://example.com/undated",
            published_at=None,
        )
    )
    worker = _worker(entries, StubGenerator(), newest_first=True)
    assert worker.process_next().entry_id == undated.id


def test_reclaim_with_zero_timeout_requeues_every_claim(
    entries: EntryRepository, source_id: int
):
    """A restart abandons in-flight work; waiting the full timeout strands it."""
    _queue(entries, source_id, "a")
    entries.claim_next()
    assert entries.get(1).status is EntryStatus.PROCESSING

    assert entries.reclaim_stale(timeout=timedelta(0)) == 1
    assert entries.get(1).status is EntryStatus.PENDING


def test_worker_passes_the_publication_as_the_byline(
    entries: EntryRepository, source_id: int
):
    entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid="a",
            title="An Article",
            article_url="https://example.com/a",
            published_at=utcnow(),
            origin_name="Daring Fireball",
        )
    )
    generator = StubGenerator()

    _worker(entries, generator).process_next()

    assert generator.bylines == ["Daring Fireball"]


# --- pause / resume --------------------------------------------------------


def test_paused_worker_claims_nothing(entries: EntryRepository, source_id: int):
    from vocast.ingest.worker import WorkerLoop

    _queue(entries, source_id, "a")
    generator = StubGenerator()
    loop = WorkerLoop(
        _worker(entries, generator), idle_seconds=0.01, is_paused=lambda: True
    )

    loop.start()
    time.sleep(0.2)
    loop.stop(timeout=5)

    assert generator.calls == []
    assert entries.get(1).status is EntryStatus.PENDING


def test_worker_resumes_when_the_flag_clears(entries: EntryRepository, source_id: int):
    from vocast.ingest.worker import WorkerLoop

    _queue(entries, source_id, "a")
    generator = StubGenerator()
    paused = {"value": True}
    loop = WorkerLoop(
        _worker(entries, generator),
        idle_seconds=0.01,
        is_paused=lambda: paused["value"],
    )
    loop.start()
    time.sleep(0.1)
    assert generator.calls == []

    paused["value"] = False
    for _ in range(100):
        if generator.calls:
            break
        time.sleep(0.02)
    loop.stop(timeout=5)

    assert len(generator.calls) == 1


def test_pause_flag_read_failure_does_not_wedge_the_worker(
    entries: EntryRepository, source_id: int
):
    from vocast.ingest.worker import WorkerLoop

    _queue(entries, source_id, "a")
    generator = StubGenerator()

    def exploding() -> bool:
        raise RuntimeError("database unavailable")

    loop = WorkerLoop(
        _worker(entries, generator), idle_seconds=0.01, is_paused=exploding
    )
    loop.start()
    for _ in range(100):
        if generator.calls:
            break
        time.sleep(0.02)
    loop.stop(timeout=5)

    assert len(generator.calls) == 1, "a flag read failure must not stop work"


def test_cancelled_generation_is_requeued_without_counting_an_attempt(
    entries: EntryRepository, source_id: int
):
    """Pausing must never push an article towards `failed`."""
    from vocast.ingest.generator import GenerationCancelled

    entry = _queue(entries, source_id, "a")
    generator = StubGenerator(results=[GenerationCancelled("paused")])

    outcome = _worker(entries, generator).process_next()

    assert outcome.retrying
    stored = entries.get(entry.id)
    assert stored.status is EntryStatus.PENDING
    assert stored.retry_count == 0
    assert stored.next_retry_at is None


def test_worker_passes_the_publication_artwork_as_the_cover(
    entries: EntryRepository, source_id: int
):
    entries.insert_if_new(
        FeedEntry(
            source_id=source_id,
            external_guid="a",
            title="An Article",
            article_url="https://example.com/a",
            published_at=utcnow(),
            origin_image_url="http://freshrss/f.php?h=abc",
        )
    )
    generator = StubGenerator()
    _worker(entries, generator).process_next()
    assert generator.covers == ["http://freshrss/f.php?h=abc"]


def test_worker_regenerates_in_place_when_an_episode_exists(
    entries: EntryRepository, source_id: int
):
    entry = _queue(entries, source_id, "a")
    entries.mark_ready(entry.id, episode_id="ep-original")
    entries.requeue(entry.id)

    generator = StubGenerator()
    _worker(entries, generator).process_next()

    assert generator.replaced == ["ep-original"]


def test_first_generation_does_not_ask_for_a_replacement(
    entries: EntryRepository, source_id: int
):
    _queue(entries, source_id, "a")
    generator = StubGenerator()
    _worker(entries, generator).process_next()
    assert generator.replaced == [None]
