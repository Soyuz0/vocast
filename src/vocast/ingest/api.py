"""HTTP surface for the ingestion service: feeds, health, and admin API.

Mounted onto the existing vocast FastAPI app, so `/feed.xml` and
`/audio/<id>.mp3` keep working exactly as before.
"""

from __future__ import annotations

import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from .adapters import supported_kinds
from .context import AppContext
from .feeds import (
    FeedChannel,
    build_podcast_rss,
    collect_episodes,
    collect_playlist_episodes,
    with_token,
)
from .freshrss_writer import FreshRSSWriter, mark_read_in_background
from .logs import get_logger, kv
from .models import EntryStatus, SourceKind
from .nethttp import BlockedURLError, validate_url
from .poller import Poller
from .public_guard import is_public_request, supplied_token
from .repository import DuplicateSourceError
from .timeutils import utcnow

log = get_logger("api")

RSS_MEDIA_TYPE = "application/rss+xml; charset=utf-8"


@dataclass
class ServiceState:
    """Runtime handles the API reports on but does not own."""

    context: AppContext
    worker_running: bool = False
    poller_running: bool = False

    def require_feed_token(self, request: Request, supplied: str | None) -> None:
        """Reject internet requests for feeds or audio without the token.

        A podcast client cannot send an Authorization header, so the secret
        travels in the URL; a browser that has already exchanged it holds a
        cookie instead. Either satisfies this, compared with compare_digest to
        avoid leaking the value through timing.

        Tailnet requests are not challenged, matching the rest of the service.
        That also keeps the token out of the library page: the page is
        unauthenticated on the tailnet, so embedding a token for its player to
        use would hand the public feed's secret to anyone who could reach it.
        """
        expected = self.context.config.server.feed_token
        if not expected or not is_public_request(request):
            return
        offered = supplied or supplied_token(request)
        if not offered or not secrets.compare_digest(offered, expected):
            raise HTTPException(401, "a valid ?token= is required for this feed")

    @property
    def feed_token(self) -> str | None:
        return self.context.config.server.feed_token

    def hide_downloaded_before(self) -> datetime | None:
        """Cutoff for dropping already-downloaded episodes from the feed."""
        hours = self.context.config.server.hide_after_download_hours
        if not hours:
            return None
        return utcnow() - timedelta(hours=hours)

    def record_download(self, episode_id: str) -> None:
        """Note an episode was fetched, and mark it read upstream if configured.

        Deliberately tolerant: this runs on the request that serves audio, so
        nothing here may raise or add latency.
        """
        try:
            entry = self.context.consumption.record_download(episode_id)
        except Exception:
            log.exception("could not record the download of %s", episode_id)
            return
        if entry is None:
            return  # already recorded; only the first fetch acts
        if not self.context.config.freshrss.mark_read_on_download:
            return
        source = self.context.sources.get(entry.source_id)
        if source is None or source.kind != SourceKind.FRESHRSS_API.value:
            return
        writer = FreshRSSWriter(source, policy=self.context.fetch_policy())
        mark_read_in_background(
            writer,
            entry.id,
            entry.external_guid,
            self.context.consumption.mark_read_upstream,
        )

    def clear_download(self, entry_id: int) -> tuple[bool, bool]:
        """Undo a download. Returns (found, still marked read upstream).

        Unlike record_download this runs from a deliberate click, so the upstream
        call is synchronous: if FreshRSS refuses, read reconciliation would quietly
        re-ignore the entry on the next full poll, and the listener should hear
        about that now rather than notice the episode vanish tomorrow.
        """
        entry = self.context.consumption.clear_download(entry_id)
        if entry is None:
            return False, False
        source = self.context.sources.get(entry.source_id)
        if source is None or source.kind != SourceKind.FRESHRSS_API.value:
            return True, False
        try:
            FreshRSSWriter(source, policy=self.context.fetch_policy()).mark_unread(
                entry.external_guid
            )
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            log.warning(
                "could not mark article unread upstream %s",
                kv(entry_id=entry_id, error=exc),
            )
            return True, True
        return True, False

    def audio_base_url(self, request: Request) -> str:
        """Where enclosures should point, which may differ from the feed host."""
        configured = self.context.config.server.audio_base_url
        return configured or self.base_url(request)

    def base_url(self, request: Request) -> str:
        """Absolute base for enclosure URLs.

        A configured public_base_url wins, because behind a TLS-terminating
        reverse proxy the request itself looks like plain http on an internal
        host, and podcast clients would be handed unreachable URLs.
        """
        configured = self.context.config.server.public_base_url
        if configured:
            return configured
        return str(request.base_url).rstrip("/")


class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    kind: str = "rss"
    enabled: bool = True
    poll_interval_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 7)
    options: dict[str, Any] = Field(default_factory=dict)


class SourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    enabled: bool | None = None
    poll_interval_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 7)
    options: dict[str, Any] | None = None


def create_router(state: ServiceState) -> APIRouter:
    router = APIRouter()
    _register_feeds(router, state)
    _register_health(router, state)
    _register_admin(router, state)
    from .library_web import register_library

    register_library(router, state)
    return router


# --- feeds -----------------------------------------------------------------


def _register_feeds(router: APIRouter, state: ServiceState) -> None:
    @router.api_route("/feeds/all.xml", methods=["GET", "HEAD"])
    def all_feed(request: Request, token: str | None = None) -> Response:
        state.require_feed_token(request, token)
        return _render_feed(state, request, source_id=None)

    @router.api_route("/feeds/recent.xml", methods=["GET", "HEAD"])
    def recent_feed(request: Request, token: str | None = None) -> Response:
        """The newest handful of episodes still waiting to be heard.

        The combined feed carries the whole backlog, and a podcast client
        re-parses every item on each refresh, so it is slow to pick up an
        addition. This one stays small enough to refresh quickly.
        """
        state.require_feed_token(request, token)
        return _render_feed(
            state,
            request,
            source_id=None,
            title="vocast \u2014 Recent",
            description="The newest narrated articles, not yet downloaded",
            max_items=state.context.config.server.recent_feed_items,
        )

    @router.api_route("/feeds/source/{source_id}.xml", methods=["GET", "HEAD"])
    def source_feed(
        source_id: int, request: Request, token: str | None = None
    ) -> Response:
        state.require_feed_token(request, token)
        source = state.context.sources.get(source_id)
        if source is None:
            return PlainTextResponse("unknown source", status_code=404)
        return _render_feed(
            state, request, source_id=source_id, source_name=source.name
        )

    @router.api_route("/feeds/listen-later.xml", methods=["GET", "HEAD"])
    def listen_later_feed(request: Request, token: str | None = None) -> Response:
        state.require_feed_token(request, token)
        base = state.base_url(request)
        episodes = collect_playlist_episodes(
            state.context.playlists,
            slug="listen-later",
            hide_downloaded_before=state.hide_downloaded_before(),
            base_url=base,
            audio_base_url=state.audio_base_url(request),
            token=state.feed_token,
            max_items=state.context.config.server.feed_max_items,
        )
        xml = build_podcast_rss(
            FeedChannel(
                title="Vocast - Listen Later",
                link=base,
                description="Articles selected for listening",
                image_url=with_token(f"{base}/cover.jpg", state.feed_token),
            ),
            episodes,
        )
        return Response(content=xml, media_type=RSS_MEDIA_TYPE)


def _render_feed(
    state: ServiceState,
    request: Request,
    *,
    source_id: int | None,
    source_name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    max_items: int | None = None,
) -> Response:
    base = state.base_url(request)
    episodes = collect_episodes(
        state.context.entries,
        base_url=base,
        source_id=source_id,
        audio_base_url=state.audio_base_url(request),
        token=state.feed_token,
        max_items=(
            max_items
            if max_items is not None
            else state.context.config.server.feed_max_items
        ),
        hide_downloaded_before=state.hide_downloaded_before(),
    )
    title = title or (f"vocast — {source_name}" if source_name else "vocast")
    description = description or (
        f"Articles from {source_name}, narrated"
        if source_name
        else "Self-hosted articles-as-podcasts"
    )
    xml = build_podcast_rss(
        FeedChannel(
            title=title,
            link=base,
            description=description,
            image_url=with_token(f"{base}/cover.jpg", state.feed_token),
        ),
        episodes,
    )
    return Response(content=xml, media_type=RSS_MEDIA_TYPE)


# --- health ----------------------------------------------------------------


def _register_health(router: APIRouter, state: ServiceState) -> None:
    @router.get("/api/health")
    def health() -> JSONResponse:
        """Report liveness without leaking configuration secrets."""
        database_ok = True
        counts: dict[str, int] = {}
        last_poll = None
        try:
            counts = state.context.entries.counts_by_status()
            last_poll = state.context.sources.last_successful_poll()
            source_count = len(state.context.sources.all())
        except Exception as exc:  # noqa: BLE001 - health must never raise
            database_ok = False
            source_count = 0
            log.warning("health check could not read the database %s", kv(error=exc))

        payload = {
            "status": "ok" if database_ok else "degraded",
            "database": "ok" if database_ok else "error",
            "worker": _worker_status(state),
            "worker_paused": _paused(state),
            "poller": "running" if state.poller_running else "stopped",
            "sources": source_count,
            "entries": counts,
            "pending": counts.get(EntryStatus.PENDING.value, 0),
            "failed": counts.get(EntryStatus.FAILED.value, 0),
            "last_successful_poll": last_poll.isoformat() if last_poll else None,
        }
        return JSONResponse(payload, status_code=200 if database_ok else 503)


# --- admin API -------------------------------------------------------------


def _worker_status(state: ServiceState) -> str:
    if not state.worker_running:
        return "stopped"
    return "paused" if _paused(state) else "running"


def _paused(state: ServiceState) -> bool:
    try:
        return state.context.settings.worker_paused
    except Exception:  # noqa: BLE001 - health must never raise
        return False


def _register_admin(router: APIRouter, state: ServiceState) -> None:
    require_admin = _admin_guard(state)

    @router.post("/api/worker/pause", dependencies=[Depends(require_admin)])
    def pause_worker() -> JSONResponse:
        """Stop claiming new articles. The one in flight still finishes."""
        state.context.settings.pause_worker(True)
        return JSONResponse({"worker_paused": True})

    @router.post("/api/worker/resume", dependencies=[Depends(require_admin)])
    def resume_worker() -> JSONResponse:
        state.context.settings.pause_worker(False)
        return JSONResponse({"worker_paused": False})

    @router.get("/api/sources", dependencies=[Depends(require_admin)])
    def list_sources() -> JSONResponse:
        return JSONResponse([_source_json(s) for s in state.context.sources.all()])

    @router.post("/api/sources", dependencies=[Depends(require_admin)])
    def create_source(payload: SourceIn) -> JSONResponse:
        if payload.kind not in supported_kinds():
            raise HTTPException(
                400, f"unknown kind {payload.kind!r}; try {supported_kinds()}"
            )
        _validate_source_url(state, payload.url, payload.options)
        interval = (
            payload.poll_interval_minutes
            or state.context.config.polling.default_interval_minutes
        )
        try:
            source = state.context.sources.add(
                name=payload.name,
                kind=payload.kind,
                url=payload.url,
                enabled=payload.enabled,
                poll_interval_minutes=interval,
                config=payload.options,
            )
        except DuplicateSourceError as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse(_source_json(source), status_code=201)

    @router.patch("/api/sources/{source_id}", dependencies=[Depends(require_admin)])
    def update_source(source_id: int, payload: SourcePatch) -> JSONResponse:
        if state.context.sources.get(source_id) is None:
            raise HTTPException(404, "unknown source")
        if payload.url is not None:
            _validate_source_url(state, payload.url, payload.options or {})
        try:
            source = state.context.sources.update(
                source_id,
                name=payload.name,
                url=payload.url,
                enabled=payload.enabled,
                poll_interval_minutes=payload.poll_interval_minutes,
                config=payload.options,
            )
        except DuplicateSourceError as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse(_source_json(source))

    @router.delete("/api/sources/{source_id}", dependencies=[Depends(require_admin)])
    def delete_source(source_id: int) -> Response:
        if not state.context.sources.remove(source_id):
            raise HTTPException(404, "unknown source")
        return Response(status_code=204)

    @router.post("/api/sources/{source_id}/poll", dependencies=[Depends(require_admin)])
    def poll_source(source_id: int) -> JSONResponse:
        source = state.context.sources.get(source_id)
        if source is None:
            raise HTTPException(404, "unknown source")
        poller = Poller(
            sources=state.context.sources,
            entries=state.context.entries,
            policy=state.context.fetch_policy(),
        )
        result = poller.poll_source(source)
        return JSONResponse(
            {
                "source_id": result.source_id,
                "discovered": result.discovered,
                "inserted": result.inserted,
                "skipped": result.skipped,
                "error": result.error,
            },
            status_code=200 if result.ok else 502,
        )

    @router.get("/api/entries", dependencies=[Depends(require_admin)])
    def list_entries_endpoint(
        status: str | None = None,
        source_id: int | None = None,
        limit: int = 100,
    ) -> JSONResponse:
        parsed_status = None
        if status is not None:
            try:
                parsed_status = EntryStatus(status)
            except ValueError as exc:
                raise HTTPException(400, f"unknown status {status!r}") from exc
        entries = state.context.entries.all(
            status=parsed_status, source_id=source_id, limit=min(max(limit, 1), 1000)
        )
        return JSONResponse([_entry_json(e) for e in entries])

    @router.post("/api/entries/{entry_id}/retry", dependencies=[Depends(require_admin)])
    def retry_entry(entry_id: int) -> JSONResponse:
        entry = state.context.entries.get(entry_id)
        if entry is None:
            raise HTTPException(404, "unknown entry")
        state.context.entries.requeue(entry_id)
        return JSONResponse({"entry_id": entry_id, "status": EntryStatus.PENDING.value})

    # Deliberately not behind the admin token. Queueing an episode is ordinary
    # use of the library, and the library is already gated: over the internet
    # the app-wide guard demands the feed token, and on the tailnet access is
    # trusted by design. Asking for a second, different secret to click a button
    # on a page you already authenticated to is friction without a threat model.
    # Same-origin is still enforced, and the administrative endpoints are
    # unchanged.
    # Same reasoning as the Listen Later buttons below: undoing a download is
    # ordinary use of an already-gated page, not an administrative action.
    @router.post("/api/entries/{entry_id}/undownload")
    def undownload_entry(entry_id: int, request: Request) -> JSONResponse:
        _require_same_origin(state, request)
        found, upstream_failed = state.clear_download(entry_id)
        if not found:
            raise HTTPException(404, "unknown entry")
        return JSONResponse({"entry_id": entry_id, "upstream_failed": upstream_failed})

    @router.post("/api/playlists/listen-later/entries/{entry_id}")
    def add_listen_later(entry_id: int, request: Request) -> JSONResponse:
        _require_same_origin(state, request)
        if state.context.entries.get(entry_id) is None:
            raise HTTPException(404, "unknown entry")
        added = state.context.playlists.add_entry("listen-later", entry_id)
        return JSONResponse(
            {"entry_id": entry_id, "queued": True, "changed": added},
            status_code=201 if added else 200,
        )

    @router.delete("/api/playlists/listen-later/entries")
    def clear_listen_later(request: Request) -> JSONResponse:
        """Empty the queue in one step, rather than one request per episode."""
        _require_same_origin(state, request)
        removed = state.context.playlists.clear("listen-later")
        return JSONResponse({"removed": removed})

    @router.delete("/api/playlists/listen-later/entries/{entry_id}")
    def remove_listen_later(entry_id: int, request: Request) -> JSONResponse:
        _require_same_origin(state, request)
        if state.context.entries.get(entry_id) is None:
            raise HTTPException(404, "unknown entry")
        removed = state.context.playlists.remove_entry("listen-later", entry_id)
        return JSONResponse({"entry_id": entry_id, "queued": False, "changed": removed})


def _admin_guard(state: ServiceState):
    """Require a bearer token for write/admin endpoints when one is configured.

    With no token configured the API stays open, which is only safe on a
    loopback bind. The README says so, and `vocast run` warns when it binds a
    non-loopback interface without a token.
    """

    def guard(authorization: str | None = Header(default=None)) -> None:
        expected = state.context.config.admin_token
        if not expected:
            return
        supplied = ""
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        import secrets

        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(401, "admin token required")

    return guard


def _require_same_origin(state: ServiceState, request: Request) -> None:
    """Block browser cross-site writes while leaving non-browser API clients usable."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(403, "cross-origin playlist changes are not allowed")
    origin = request.headers.get("origin")
    if not origin:
        return
    allowed = {str(request.base_url).rstrip("/")}
    configured = state.context.config.server.public_base_url
    if configured:
        configured_url = urllib.parse.urlsplit(configured)
        allowed.add(f"{configured_url.scheme}://{configured_url.netloc}")
    parsed = urllib.parse.urlsplit(origin)
    normalized = (
        f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    )
    if normalized not in allowed:
        raise HTTPException(403, "cross-origin playlist changes are not allowed")


def _validate_source_url(
    state: ServiceState, url: str, options: dict[str, Any]
) -> None:
    allow_private = bool(
        options.get("allow_private_urls", state.context.config.allow_private_urls)
    )
    try:
        validate_url(url, allow_private=allow_private)
    except BlockedURLError as exc:
        raise HTTPException(400, str(exc)) from exc


def _source_json(source) -> dict[str, Any]:
    return {
        "id": source.id,
        "name": source.name,
        "kind": source.kind,
        "url": source.url,
        "enabled": source.enabled,
        "poll_interval_minutes": source.poll_interval_minutes,
        "last_checked_at": _iso(source.last_checked_at),
        "last_success_at": _iso(source.last_success_at),
        "last_error": source.last_error,
        "feed": f"/feeds/source/{source.id}.xml",
    }


def _entry_json(entry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "source_id": entry.source_id,
        "title": entry.title,
        "article_url": entry.article_url,
        "status": entry.status.value,
        "episode_id": entry.vocast_episode_id,
        "retry_count": entry.retry_count,
        "published_at": _iso(entry.published_at),
        "next_retry_at": _iso(entry.next_retry_at),
        "error_message": entry.error_message,
    }


def _iso(value) -> str | None:
    return value.isoformat() if value else None
