"""A background thread that runs a callable on an interval."""

from __future__ import annotations

import threading
from collections.abc import Callable

from .logs import get_logger

log = get_logger("loops")


class IntervalLoop:
    """Calls `tick` every `interval_seconds` until stopped.

    A raised exception is logged and the loop continues: a scheduler that dies
    on the first bad feed would silently stop all future work.
    """

    def __init__(
        self,
        tick: Callable[[], None],
        *,
        interval_seconds: float,
        name: str,
        run_immediately: bool = True,
    ) -> None:
        self._tick = tick
        self._interval_seconds = interval_seconds
        self._name = name
        self._run_immediately = run_immediately
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        log.info("%s started (every %ss)", self._name, self._interval_seconds)
        if not self._run_immediately:
            self._stop.wait(self._interval_seconds)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("%s tick failed", self._name)
            # Waiting on the stop event (rather than sleeping) means shutdown
            # is immediate instead of blocking for a full interval.
            self._stop.wait(self._interval_seconds)
        log.info("%s stopped", self._name)
