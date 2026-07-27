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
            lines.append("library: /library")
            lines.append("listen later: /feeds/listen-later.xml")
            lines.append("health: /api/health")
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.api_route("/feed.xml", methods=["GET", "HEAD"])
    def feed(request: Request, token: str | None = None) -> Response:
        """The original feed. An alias for /feeds/all.xml once ingestion is on,
        so a subscriber added before RSS support keeps getting every episode."""
        if state is not None:
            state.require_feed_token(request, token)
        base = _base_url(state, request)
        xml = _render_all(state, base, request)
        return Response(content=xml, media_type="application/rss+xml; charset=utf-8")

    @app.api_route("/audio/{entry_id}.mp3", methods=["GET", "HEAD"])
    def audio(request: Request, entry_id: str, token: str | None = None) -> Response:
        if state is not None:
            state.require_feed_token(request, token)
        entry = get_entry(entry_id)
        if entry is None:
            return PlainTextResponse("not found", status_code=404)
        path = entry.audio_path()
        if not path.exists():
            return PlainTextResponse("audio missing", status_code=404)
        if state is not None and not _is_probe(request):
            state.record_download(entry_id)
        return FileResponse(path, media_type="audio/mpeg")

    @app.api_route("/cover.jpg", methods=["GET", "HEAD"])
    def cover(request: Request, token: str | None = None) -> Response:
        if state is not None:
            state.require_feed_token(request, token)
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
        from .ingest.public_guard import public_access_denied

        @app.middleware("http")
        async def require_token_from_the_internet(request: Request, call_next):
            """Gate everything published through Tailscale Funnel.

            Applied here rather than per route so a route added later is covered
            by default. Funnel exposes every path, so the safe default is that
            reaching the service from the internet requires the token.
            """
            denied = public_access_denied(
                state.context.config.server.feed_token or "", request
            )
            if denied is not None:
                return denied
            return await call_next(request)

        app.include_router(create_router(state))

    return app


def _is_probe(request: Request) -> bool:
    """Whether this request is a client checking rather than downloading.

    HEAD requests and small range probes are how clients read metadata or test
    resumability; treating those as a download would mark articles consumed
    that were never fetched.
    """
    if request.method.upper() == "HEAD":
        return True
    span = request.headers.get("range", "")
    if not span.startswith("bytes="):
        return False
    first, separator, last = span[len("bytes=") :].partition("-")
    if not separator:
        return False
    if not first:
        # A suffix range, "bytes=-1024": the last N bytes, which is how a client
        # reads trailing tags. Small ones are probes like any other.
        return last.isdigit() and int(last) < 65536
    if not last:
        return False  # open-ended from an offset: the rest of the file
    if not first.isdigit() or not last.isdigit():
        return False
    return (int(last) - int(first)) < 65536


def _base_url(state: ServiceState | None, request: Request) -> str:
    if state is not None:
        return state.base_url(request)
    return str(request.base_url).rstrip("/")


def _render_all(
    state: ServiceState | None, base_url: str, request: Request | None = None
) -> str:
    from .ingest.feeds import (
        FeedChannel,
        build_podcast_rss,
        collect_episodes,
        with_token,
    )

    entries = state.context.entries if state is not None else None
    token = state.feed_token if state is not None else None
    audio_base = (
        state.audio_base_url(request)
        if state is not None and request is not None
        else None
    )
    max_items = (
        state.context.config.server.feed_max_items if state is not None else None
    )
    episodes = collect_episodes(
        entries,
        base_url=base_url,
        audio_base_url=audio_base,
        token=token,
        max_items=max_items,
        # Without this the alias keeps listing episodes the combined feed has
        # retired, so the two disagree for exactly the long-standing
        # subscribers the alias exists to serve.
        hide_read_before=state.hide_read_before() if state is not None else None,
    )
    cover = with_token(f"{base_url}/cover.jpg", token) if _SHOW_COVER else None
    return build_podcast_rss(
        FeedChannel(
            title="vocast",
            link=base_url,
            description="Self-hosted articles-as-podcasts",
            image_url=cover,
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
    except Exception:
        import logging

        logging.getLogger("vocast.server").warning(
            "ingestion routes unavailable; serving the library-only feed",
            exc_info=True,
        )
        return None
