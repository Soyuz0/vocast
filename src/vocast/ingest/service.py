"""The combined service: HTTP server plus background poller and worker.

Everything runs in one process and shares one SQLite database. The poller and
worker live on daemon threads; uvicorn owns the main thread and its lifespan
hooks start and stop them, so Ctrl-C and `docker stop` both shut down cleanly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from .api import ServiceState
from .config import Config
from .context import AppContext
from .logs import get_logger, kv
from .loops import IntervalLoop
from .poller import Poller
from .retention import Retention
from .storage import verify_storage
from .tuning import apply_compute_threads
from .worker import Worker, WorkerLoop

log = get_logger("service")


#: The scheduler wakes up far more often than any poll interval; `due()` is
#: what decides which sources are actually fetched, so per-source intervals are
#: honored to within one tick.
POLL_TICK_SECONDS = 30.0

RETENTION_TICK_SECONDS = 3600.0


def build_poller_loop(poller: Poller) -> IntervalLoop:
    def tick() -> None:
        report = poller.poll_due()
        if report.inserted:
            log.info(
                "poll cycle queued articles %s",
                kv(inserted=report.inserted, sources=report.polled),
            )

    return IntervalLoop(tick, interval_seconds=POLL_TICK_SECONDS, name="vocast-poller")


class Service:
    """Owns the runtime pieces and their lifecycle."""

    def __init__(
        self,
        context: AppContext,
        *,
        with_poller: bool = True,
        with_worker: bool = True,
    ) -> None:
        self.context = context
        self.state = ServiceState(context=context)
        self._poller_loop: IntervalLoop | None = None
        self._full_poll_loop: IntervalLoop | None = None
        self._retention_loop: IntervalLoop | None = None
        self._worker_loops: list[WorkerLoop] = []
        self._with_poller = with_poller
        self._with_worker = with_worker
        self._shutting_down = False

    def start(self) -> None:
        self._shutting_down = False
        config = self.context.config
        if self._with_poller:
            poller = Poller(
                sources=self.context.sources,
                entries=self.context.entries,
                policy=self.context.fetch_policy(),
            )
            self._poller_loop = build_poller_loop(poller)
            self._poller_loop.start()
            self.state.poller_running = True

        if self._with_poller and config.polling.full_poll_hours:
            self._full_poll_loop = IntervalLoop(
                self._full_poll,
                interval_seconds=config.polling.full_poll_hours * 3600,
                name="vocast-full-poll",
                # Not on startup: the routine poller is already running and a
                # full walk of a large backlog is expensive.
                run_immediately=False,
            )
            self._full_poll_loop.start()

        if config.retention.enabled:
            self._retention_loop = self._build_retention_loop()
            self._retention_loop.start()

        if self._with_worker:
            # Before any engine is built: thread pools size themselves on first
            # use, so this cannot be corrected later.
            apply_compute_threads(
                config.worker.concurrency, config.worker.threads_per_worker
            )

        if self._with_worker and config.worker.reclaim_on_start:
            # Done once, before any worker starts: at this point nothing in
            # this process holds a claim, so anything still marked processing
            # was abandoned by a previous run. Doing it per worker would let
            # one steal a sibling's freshly claimed entry.
            from datetime import timedelta

            recovered = self.context.entries.reclaim_stale(timeout=timedelta(0))
            if recovered:
                log.warning(
                    "requeued entries abandoned by a previous run %s",
                    kv(count=recovered),
                )

        if self._with_worker:
            # One generator per worker: each holds its own TTS engine, and
            # engines are not documented as thread-safe.
            for index in range(max(1, config.worker.concurrency)):
                loop = WorkerLoop(
                    self._build_worker(),
                    name=f"vocast-worker-{index + 1}",
                    is_paused=lambda: self.context.settings.worker_paused,
                    nice=config.worker.nice,
                )
                loop.start()
                self._worker_loops.append(loop)
            self.state.worker_running = True

    def _full_poll(self) -> None:
        """Walk every source completely, so read reconciliation can run."""
        poller = Poller(
            sources=self.context.sources,
            entries=self.context.entries,
            policy=self.context.fetch_policy(),
        )
        for source in self.context.sources.all(enabled_only=True):
            result = poller.poll_source(source, full=True)
            if result.inserted or result.ignored:
                log.info(
                    "full poll %s",
                    kv(
                        source_id=source.id,
                        inserted=result.inserted,
                        ignored=result.ignored,
                    ),
                )

    def _build_retention_loop(self) -> IntervalLoop:
        retention = Retention(
            entries=self.context.entries,
            config=self.context.config.retention,
            library_path=self.context.config.storage.library_path,
            include_manual=self.context.config.retention.include_manual,
        )

        def sweep() -> None:
            retention.apply()

        return IntervalLoop(
            sweep,
            interval_seconds=RETENTION_TICK_SECONDS,
            name="vocast-retention",
        )

    def _build_worker(self) -> Worker:
        from .generator import VocastEpisodeGenerator

        generator = VocastEpisodeGenerator(
            engine_name=self.context.config.tts.engine,
            voice=self.context.config.tts.voice,
            policy=self.context.fetch_policy(),
            # Checked between chunks, so pausing interrupts a long article
            # instead of waiting out what can be hours of synthesis.
            should_continue=self._keep_synthesizing,
        )
        return Worker(
            entries=self.context.entries,
            generator=generator,
            config=self.context.config.worker,
        )

    def _keep_synthesizing(self) -> bool:
        """False once narration is paused or the service is shutting down."""
        if self._shutting_down:
            return False
        try:
            return not self.context.settings.worker_paused
        except Exception:  # noqa: BLE001 - never abort work over a read error
            return True

    def stop(self) -> None:
        """Stop background work, abandoning any in-progress synthesis.

        Cancelled articles are requeued, so nothing is lost beyond the partial
        audio, which is never written to disk.
        """
        self._shutting_down = True
        if self._poller_loop is not None:
            self._poller_loop.stop()
            self.state.poller_running = False
        if self._full_poll_loop is not None:
            self._full_poll_loop.stop()
            self._full_poll_loop = None
        if self._retention_loop is not None:
            self._retention_loop.stop()
            self._retention_loop = None
        for loop in self._worker_loops:
            loop.stop()
        self._worker_loops.clear()
        self.state.worker_running = False

    def refresh_status(self) -> None:
        self.state.poller_running = bool(
            self._poller_loop and self._poller_loop.running
        )
        self.state.worker_running = any(loop.running for loop in self._worker_loops)


def create_service_app(service: Service):
    """Build the FastAPI app, tying background threads to its lifespan."""
    from ..server import create_app

    @asynccontextmanager
    async def lifespan(app):
        service.start()
        try:
            yield
        finally:
            service.stop()

    app = create_app(service.state)
    app.router.lifespan_context = lifespan
    return app


def run_service(
    config: Config,
    *,
    host: str | None = None,
    port: int | None = None,
    with_poller: bool = True,
    with_worker: bool = True,
) -> int:
    import uvicorn

    # Before anything else: if we cannot store episodes there is no point
    # discovering or synthesizing them.
    verify_storage(config.storage)

    context = AppContext.create(config)
    context.sync_configured_sources()
    service = Service(context, with_poller=with_poller, with_worker=with_worker)
    app = create_service_app(service)

    bind_host = host or config.server.host
    bind_port = port or config.server.port
    _warn_if_exposed_without_a_token(config, bind_host)

    log.info(
        "starting vocast %s",
        kv(
            host=bind_host,
            port=bind_port,
            poller=with_poller,
            worker=with_worker,
            public_base_url=config.server.public_base_url,
        ),
    )
    uvicorn.run(app, host=bind_host, port=bind_port, log_level=config.log_level.lower())
    context.close()
    return 0


def _warn_if_exposed_without_a_token(config: Config, host: str) -> None:
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not config.admin_token:
        log.warning(
            "admin API is reachable without a token %s",
            kv(
                host=host,
                hint="set VOCAST_ADMIN_TOKEN, or keep the port private",
            ),
        )
