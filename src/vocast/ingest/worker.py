"""The queue worker — claims pending articles and generates episodes.

The database is the queue. A worker claims one entry at a time inside a
transaction, so adding workers later needs no change to the data model and no
external broker.
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import WorkerConfig
from .generator import (
    EpisodeGenerator,
    GeneratedEpisode,
    GenerationCancelled,
    PermanentGenerationError,
    TransientGenerationError,
)
from .logs import get_logger, kv
from .models import Entry
from .repository import EntryRepository
from .timeutils import utcnow

log = get_logger("worker")


@dataclass
class WorkOutcome:
    entry_id: int
    episode_id: str | None = None
    error: str | None = None
    retrying: bool = False

    @property
    def ok(self) -> bool:
        return self.episode_id is not None


class Worker:
    """Processes queued entries one at a time."""

    def __init__(
        self,
        *,
        entries: EntryRepository,
        generator: EpisodeGenerator,
        config: WorkerConfig | None = None,
    ) -> None:
        self._entries = entries
        self._generator = generator
        self._config = config or WorkerConfig()

    @property
    def config(self) -> WorkerConfig:
        return self._config

    def reclaim_stale(self, *, now: datetime | None = None) -> int:
        """Requeue entries whose worker died mid-generation."""
        recovered = self._entries.reclaim_stale(
            timeout=timedelta(minutes=self._config.processing_timeout_minutes), now=now
        )
        if recovered:
            log.warning(
                "reclaimed abandoned entries %s",
                kv(
                    count=recovered,
                    timeout_minutes=self._config.processing_timeout_minutes,
                ),
            )
        return recovered

    def process_next(self, *, now: datetime | None = None) -> WorkOutcome | None:
        """Claim and process one entry. Returns None when nothing is due."""
        entry = self._entries.claim_next(
            now=now, newest_first=self._config.newest_first
        )
        if entry is None:
            return None
        return self._process(entry)

    def drain(self, *, max_entries: int | None = None) -> list[WorkOutcome]:
        """Process until the queue is empty or max_entries is reached.

        Entries scheduled for a future retry are not "due", so a queue holding
        only backed-off entries counts as empty and drain returns.
        """
        outcomes: list[WorkOutcome] = []
        while max_entries is None or len(outcomes) < max_entries:
            outcome = self.process_next()
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    # -- internals ---------------------------------------------------------

    def _process(self, entry: Entry) -> WorkOutcome:
        log.info(
            "generating %s",
            kv(
                entry_id=entry.id,
                source_id=entry.source_id,
                url=entry.article_url,
                stage="generate",
                retry_count=entry.retry_count,
            ),
        )
        try:
            episode = self._generator.generate_from_url(
                entry.article_url,
                title=entry.title,
                byline=entry.origin_name,
                cover_url=entry.origin_image_url,
                replace_episode_id=entry.vocast_episode_id,
                content_html=entry.feed_content,
                on_progress=functools.partial(self._record_progress, entry.id),
            )
        except GenerationCancelled as exc:
            # Deliberate stop: requeue without counting an attempt, so pausing
            # never pushes an article towards `failed`.
            self._entries.requeue(entry.id, reset_retries=False)
            log.info(
                "generation cancelled, requeued %s",
                kv(entry_id=entry.id, url=entry.article_url, reason=str(exc)),
            )
            return WorkOutcome(entry_id=entry.id, error=str(exc), retrying=True)
        except PermanentGenerationError as exc:
            return self._fail_permanently(entry, str(exc))
        except TransientGenerationError as exc:
            return self._handle_transient(entry, str(exc))
        except Exception as exc:  # noqa: BLE001
            # An unclassified failure is treated as transient: retrying a
            # handful of times is cheaper than silently dropping an article,
            # and it still lands in `failed` once attempts run out.
            return self._handle_transient(entry, f"{type(exc).__name__}: {exc}")

        return self._succeed(entry, episode)

    def _record_progress(self, entry_id: int, done: int, total: int) -> None:
        """Persist chunk progress, ignoring failures.

        Called once per chunk purely so the library can show a bar; losing an
        update is not worth interrupting synthesis for.
        """
        try:
            self._entries.record_progress(entry_id, done, total)
        except Exception:  # noqa: BLE001 - cosmetic telemetry only
            log.debug("could not record progress for entry %s", entry_id)

    def _succeed(self, entry: Entry, episode: GeneratedEpisode) -> WorkOutcome:
        self._entries.mark_ready(
            entry.id,
            episode_id=episode.episode_id,
            content_hash=episode.content_hash,
            duration_seconds=episode.duration_seconds,
            audio_bytes=episode.audio_bytes,
        )
        log.info(
            "ready %s",
            kv(
                entry_id=entry.id,
                source_id=entry.source_id,
                episode_id=episode.episode_id,
                url=entry.article_url,
                stage="ready",
                seconds=(
                    round(episode.duration_seconds, 1)
                    if episode.duration_seconds is not None
                    else None
                ),
            ),
        )
        return WorkOutcome(entry_id=entry.id, episode_id=episode.episode_id)

    def _handle_transient(self, entry: Entry, error: str) -> WorkOutcome:
        attempts = entry.retry_count + 1
        if attempts >= self._config.max_retries:
            log.error(
                "giving up %s",
                kv(
                    entry_id=entry.id,
                    source_id=entry.source_id,
                    url=entry.article_url,
                    stage="generate",
                    retry_count=attempts,
                    error=error,
                ),
            )
            self._entries.mark_failed(entry.id, error=error)
            return WorkOutcome(entry_id=entry.id, error=error)

        delay = self.retry_delay(attempts)
        self._entries.schedule_retry(
            entry.id, error=error, next_retry_at=utcnow() + delay
        )
        log.warning(
            "retrying %s",
            kv(
                entry_id=entry.id,
                source_id=entry.source_id,
                url=entry.article_url,
                stage="generate",
                retry_count=attempts,
                retry_in_minutes=round(delay.total_seconds() / 60, 1),
                error=error,
            ),
        )
        return WorkOutcome(entry_id=entry.id, error=error, retrying=True)

    def _fail_permanently(self, entry: Entry, error: str) -> WorkOutcome:
        log.error(
            "permanent failure %s",
            kv(
                entry_id=entry.id,
                source_id=entry.source_id,
                url=entry.article_url,
                stage="generate",
                retry_count=entry.retry_count,
                error=error,
            ),
        )
        self._entries.mark_failed(entry.id, error=error)
        return WorkOutcome(entry_id=entry.id, error=error)

    def retry_delay(self, attempt: int) -> timedelta:
        """Exponential backoff, capped: base * 2^(attempt-1), max max_retry."""
        base = max(1, self._config.base_retry_minutes)
        ceiling = max(base, self._config.max_retry_minutes)
        minutes = min(base * (2 ** max(0, attempt - 1)), ceiling)
        return timedelta(minutes=minutes)


class WorkerLoop:
    """Runs a Worker on a background thread until asked to stop."""

    def __init__(
        self,
        worker: Worker,
        *,
        idle_seconds: float = 5.0,
        name: str = "vocast-worker",
        is_paused: Callable[[], bool] | None = None,
        nice: int = 0,
    ) -> None:
        self._worker = worker
        self._idle_seconds = idle_seconds
        self._name = name
        self._is_paused = is_paused
        self._nice = nice
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def stop(self, timeout: float = 30.0) -> None:
        """Signal the loop to finish and wait for the in-flight episode.

        The worker is not interrupted mid-synthesis; it finishes the current
        article so a half-written mp3 is never left behind.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        self._apply_nice()
        log.info("worker started %s", kv(name=self._name, nice=self._nice or None))
        self._worker.reclaim_stale()
        was_paused = False
        while not self._stop.is_set():
            if self._paused():
                if not was_paused:
                    log.info("worker paused %s", kv(name=self._name))
                    was_paused = True
                self._stop.wait(self._idle_seconds)
                continue
            if was_paused:
                log.info("worker resumed %s", kv(name=self._name))
                was_paused = False
            try:
                self._busy.set()
                outcome = self._worker.process_next()
            except Exception:
                log.exception("worker loop error")
                outcome = None
            finally:
                self._busy.clear()
            if outcome is None:
                # Nothing due; wait, but wake immediately on shutdown.
                self._stop.wait(self._idle_seconds)
        log.info("worker stopped %s", kv(name=self._name))

    def _paused(self) -> bool:
        if self._is_paused is None:
            return False
        try:
            return self._is_paused()
        except Exception:
            log.exception("could not read the pause flag; continuing")
            return False

    def _apply_nice(self) -> None:
        """Lower this thread's scheduling priority.

        Synthesis is CPU-bound and will otherwise compete with interactive work.
        On Linux nice(2) applies per-thread, so only the worker is deprioritized
        and the HTTP server stays responsive.
        """
        if not self._nice:
            return
        try:
            os.nice(self._nice)
        except (OSError, AttributeError) as exc:
            log.warning("could not lower worker priority %s", kv(error=exc))
