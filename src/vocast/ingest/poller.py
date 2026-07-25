"""Feed polling — discover new articles and enqueue them.

The poller only ever writes rows. It never extracts an article and never calls
a TTS engine, so a slow or broken feed can delay discovery but can never stall
episode generation, and vice versa.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .adapters import SourceAdapter, build_adapter
from .logs import get_logger, kv
from .models import Entry, Source
from .nethttp import FetchPolicy
from .repository import EntryRepository, SourceRepository
from .timeutils import utcnow

log = get_logger("poller")

AdapterFactory = Callable[..., SourceAdapter]


@dataclass
class SourcePollResult:
    source_id: int
    source_name: str
    discovered: int = 0
    inserted: int = 0
    error: str | None = None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class PollReport:
    results: list[SourcePollResult] = field(default_factory=list)

    @property
    def inserted(self) -> int:
        return sum(r.inserted for r in self.results)

    @property
    def failures(self) -> list[SourcePollResult]:
        return [r for r in self.results if not r.ok]

    @property
    def polled(self) -> int:
        return sum(1 for r in self.results if not r.skipped)


class Poller:
    def __init__(
        self,
        *,
        sources: SourceRepository,
        entries: EntryRepository,
        adapter_factory: AdapterFactory = build_adapter,
        policy: FetchPolicy | None = None,
    ) -> None:
        self._sources = sources
        self._entries = entries
        self._adapter_factory = adapter_factory
        self._policy = policy
        # Guards against a long poll overlapping the next scheduled tick, or a
        # manual `vocast poll` racing the background scheduler in-process.
        self._in_flight: set[int] = set()
        self._lock = threading.Lock()

    def poll_due(self, *, now: datetime | None = None) -> PollReport:
        moment = now or utcnow()
        report = PollReport()
        for source in self._sources.due(now=moment):
            report.results.append(self.poll_source(source))
        return report

    def poll_all(self, *, enabled_only: bool = True) -> PollReport:
        report = PollReport()
        for source in self._sources.all(enabled_only=enabled_only):
            report.results.append(self.poll_source(source))
        return report

    def poll_source(self, source: Source) -> SourcePollResult:
        """Fetch one source and insert whatever is new.

        Never raises: a single misbehaving source must not stop the others, so
        every failure is recorded on the source row and returned in the result.
        """
        result = SourcePollResult(source_id=source.id, source_name=source.name)
        if not self._begin(source.id):
            result.skipped = True
            log.info(
                "poll skipped, already in flight %s",
                kv(source_id=source.id, source=source.name),
            )
            return result
        try:
            self._poll_locked(source, result)
        finally:
            self._end(source.id)
        return result

    # -- internals ---------------------------------------------------------

    def _poll_locked(self, source: Source, result: SourcePollResult) -> None:
        try:
            adapter = self._adapter_factory(
                source,
                policy=self._policy,
                known_guids=functools.partial(self._entries.known_guids, source.id),
            )
            discovered = adapter.fetch_entries()
        # Adapters wrap third-party parsers and sockets, so the failure surface
        # is open-ended. The error is recorded and returned, never discarded.
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            result.error = message
            self._sources.mark_error(source.id, message)
            log.warning(
                "poll failed %s",
                kv(
                    source_id=source.id,
                    source=source.name,
                    url=source.url,
                    stage="fetch",
                    error=message,
                ),
            )
            return

        result.discovered = len(discovered)
        for feed_entry in discovered:
            inserted = self._insert(source, feed_entry)
            if inserted is not None:
                result.inserted += 1

        self._sources.mark_success(source.id)
        log.info(
            "poll ok %s",
            kv(
                source_id=source.id,
                source=source.name,
                discovered=result.discovered,
                inserted=result.inserted,
            ),
        )

    def _insert(self, source: Source, feed_entry) -> Entry | None:
        try:
            entry = self._entries.insert_if_new(feed_entry)
        except Exception as exc:  # noqa: BLE001
            # One malformed item should not discard the rest of the feed.
            log.warning(
                "entry insert failed %s",
                kv(
                    source_id=source.id,
                    source=source.name,
                    url=feed_entry.article_url,
                    stage="insert",
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )
            return None
        if entry is not None:
            log.info(
                "entry queued %s",
                kv(
                    source_id=source.id,
                    source=source.name,
                    entry_id=entry.id,
                    url=entry.article_url,
                    title=entry.title,
                ),
            )
        return entry

    def _begin(self, source_id: int) -> bool:
        with self._lock:
            if source_id in self._in_flight:
                return False
            self._in_flight.add(source_id)
            return True

    def _end(self, source_id: int) -> None:
        with self._lock:
            self._in_flight.discard(source_id)
