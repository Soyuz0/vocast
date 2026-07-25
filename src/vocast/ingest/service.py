"""The combined service: HTTP server plus background poller and worker.

Everything runs in one process and shares one SQLite database. The poller and
worker live on daemon threads; uvicorn owns the main thread and its lifespan
hooks start and stop them, so Ctrl-C and `docker stop` both shut down cleanly.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from .api import ServiceState
from .config import Config
from .context import AppContext
from .logs import get_logger, kv
from .poller import Poller
from .worker import Worker, WorkerLoop

log = get_logger("service")


class PollerLoop:
    """Polls due sources on an interval until asked to stop."""

    def __init__(
        self,
        poller: Poller,
        *,
        tick_seconds: float = 30.0,
        name: str = "vocast-poller",
    ) -> None:
        self._poller = poller
        self._tick_seconds = tick_seconds
        self._name = name
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

    def _run(self) -> None:
        log.info("poller started %s", kv(tick_seconds=self._tick_seconds))
        while not self._stop.is_set():
            try:
                report = self._poller.poll_due()
                if report.inserted:
                    log.info(
                        "poll cycle queued articles %s",
                        kv(inserted=report.inserted, sources=report.polled),
                    )
            except Exception:
                # The scheduler must survive anything a source throws, or
                # discovery silently stops for the life of the process.
                log.exception("poll cycle failed")
            # Ticking more often than the poll interval is what lets per-source
            # intervals be honored; `due()` decides what actually gets fetched.
            self._stop.wait(self._tick_seconds)
        log.info("poller stopped")


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
        self._poller_loop: PollerLoop | None = None
        self._worker_loops: list[WorkerLoop] = []
        self._with_poller = with_poller
        self._with_worker = with_worker

    def start(self) -> None:
        config = self.context.config
        if self._with_poller:
            poller = Poller(
                sources=self.context.sources,
                entries=self.context.entries,
                policy=self.context.fetch_policy(),
            )
            self._poller_loop = PollerLoop(poller)
            self._poller_loop.start()
            self.state.poller_running = True

        if self._with_worker:
            # One generator per worker: each holds its own TTS engine, and
            # engines are not documented as thread-safe.
            for index in range(max(1, config.worker.concurrency)):
                loop = WorkerLoop(
                    self._build_worker(), name=f"vocast-worker-{index + 1}"
                )
                loop.start()
                self._worker_loops.append(loop)
            self.state.worker_running = True

    def _build_worker(self) -> Worker:
        from .generator import VocastEpisodeGenerator

        generator = VocastEpisodeGenerator(
            engine_name=self.context.config.tts.engine,
            voice=self.context.config.tts.voice,
            policy=self.context.fetch_policy(),
        )
        return Worker(
            entries=self.context.entries,
            generator=generator,
            config=self.context.config.worker,
        )

    def stop(self) -> None:
        """Stop background work, letting an in-flight episode finish."""
        if self._poller_loop is not None:
            self._poller_loop.stop()
            self.state.poller_running = False
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
