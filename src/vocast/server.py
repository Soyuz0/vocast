"""HTTP server — exposes the library as a podcast RSS feed + audio endpoints."""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from .library import LibraryEntry, get_entry, list_entries

if TYPE_CHECKING:
    from .ingest.api import ServiceState

try:
    _SHOW_COVER = files("vocast").joinpath("assets/default_cover.jpg").read_bytes()
except (FileNotFoundError, OSError):
    _SHOW_COVER = b""


def create_app(state: ServiceState | None = None) -> FastAPI:
    """Build the app.

    Without a ServiceState the app serves only the library, which is what plain
    `vocast serve` needs. With one, the ingestion feeds, health endpoint, and
    admin API are mounted too.
    """
    app = FastAPI(title="vocast", docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> Response:
        n = len(list_entries())
        suffix = "s" if n != 1 else ""
        lines = [f"vocast — {n} article{suffix}", "feed: /feed.xml"]
        if state is not None:
            lines.append("all sources: /feeds/all.xml")
            lines.append("health: /api/health")
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.api_route("/feed.xml", methods=["GET", "HEAD"])
    def feed(request: Request) -> Response:
        """The original feed. An alias for /feeds/all.xml once ingestion is on,
        so a subscriber added before RSS support keeps getting every episode."""
        base = _base_url(state, request)
        xml = _render_all(state, base)
        return Response(content=xml, media_type="application/rss+xml; charset=utf-8")

    @app.api_route("/audio/{entry_id}.mp3", methods=["GET", "HEAD"])
    def audio(entry_id: str) -> Response:
        entry = get_entry(entry_id)
        if entry is None:
            return PlainTextResponse("not found", status_code=404)
        path = entry.audio_path()
        if not path.exists():
            return PlainTextResponse("audio missing", status_code=404)
        return FileResponse(path, media_type="audio/mpeg")

    @app.api_route("/cover.jpg", methods=["GET", "HEAD"])
    def cover() -> Response:
        if not _SHOW_COVER:
            return PlainTextResponse("not found", status_code=404)
        return Response(_SHOW_COVER, media_type="image/jpeg")

    # Downcast probes the host for a site icon to use as show art; send it here.
    @app.get("/favicon.ico")
    @app.get("/apple-touch-icon.png")
    @app.get("/apple-touch-icon-precomposed.png")
    def site_icon() -> Response:
        return RedirectResponse("/cover.jpg")

    if state is not None:
        from .ingest.api import create_router

        app.include_router(create_router(state))

    return app


def _base_url(state: ServiceState | None, request: Request) -> str:
    if state is not None:
        return state.base_url(request)
    return str(request.base_url).rstrip("/")


def _render_all(state: ServiceState | None, base_url: str) -> str:
    from .ingest.feeds import FeedChannel, build_podcast_rss, collect_episodes

    entries = state.context.entries if state is not None else None
    episodes = collect_episodes(entries, base_url=base_url)
    return build_podcast_rss(
        FeedChannel(
            title="vocast",
            link=base_url,
            description="Self-hosted articles-as-podcasts",
            image_url=f"{base_url}/cover.jpg" if _SHOW_COVER else None,
        ),
        episodes,
    )


def _build_rss(entries: list[LibraryEntry], base_url: str) -> str:
    """Render a feed from library entries alone, without ingestion provenance."""
    from .ingest.feeds import (
        FeedChannel,
        build_podcast_rss,
        library_entries_to_episodes,
    )

    return build_podcast_rss(
        FeedChannel(
            title="vocast",
            link=base_url,
            description="Self-hosted articles-as-podcasts",
            image_url=f"{base_url}/cover.jpg" if _SHOW_COVER else None,
        ),
        library_entries_to_episodes(entries, base_url=base_url),
    )


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    import uvicorn

    uvicorn.run(create_app(_optional_state()), host=host, port=port, log_level="info")


def _optional_state() -> ServiceState | None:
    """Attach ingestion routes if the database can be opened.

    `vocast serve` predates ingestion, so it must keep working even when the
    database or config is unusable; in that case the library-only feed is
    served rather than failing to start.
    """
    try:
        from .ingest.api import ServiceState
        from .ingest.context import AppContext

        return ServiceState(context=AppContext.create())
    except Exception:  # noqa: BLE001 - never let ingestion break `vocast serve`
        import logging

        logging.getLogger("vocast.server").warning(
            "ingestion routes unavailable; serving the library-only feed",
            exc_info=True,
        )
        return None
