"""HTTP surface for the ingestion service: feeds, health, and admin API.

Mounted onto the existing vocast FastAPI app, so `/feed.xml` and
`/audio/<id>.mp3` keep working exactly as before.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from .adapters import supported_kinds
from .context import AppContext
from .feeds import FeedChannel, build_podcast_rss, collect_episodes, with_token
from .logs import get_logger, kv
from .models import EntryStatus
from .nethttp import BlockedURLError, validate_url
from .poller import Poller
from .repository import DuplicateSourceError

log = get_logger("api")

RSS_MEDIA_TYPE = "application/rss+xml; charset=utf-8"


@dataclass
class ServiceState:
    """Runtime handles the API reports on but does not own."""

    context: AppContext
    worker_running: bool = False
    poller_running: bool = False

    def require_feed_token(self, supplied: str | None) -> None:
        """Reject feed/audio requests without the configured token.

        A podcast client cannot send an Authorization header, so the secret has
        to travel in the URL. Compared with compare_digest to avoid leaking it
        through timing.
        """
        expected = self.context.config.server.feed_token
        if not expected:
            return
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(401, "a valid ?token= is required for this feed")

    @property
    def feed_token(self) -> str | None:
        return self.context.config.server.feed_token

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
    return router


# --- feeds -----------------------------------------------------------------


def _register_feeds(router: APIRouter, state: ServiceState) -> None:
    @router.api_route("/feeds/all.xml", methods=["GET", "HEAD"])
    def all_feed(request: Request, token: str | None = None) -> Response:
        state.require_feed_token(token)
        return _render_feed(state, request, source_id=None)

    @router.api_route("/feeds/source/{source_id}.xml", methods=["GET", "HEAD"])
    def source_feed(
        source_id: int, request: Request, token: str | None = None
    ) -> Response:
        state.require_feed_token(token)
        source = state.context.sources.get(source_id)
        if source is None:
            return PlainTextResponse("unknown source", status_code=404)
        return _render_feed(
            state, request, source_id=source_id, source_name=source.name
        )


def _render_feed(
    state: ServiceState,
    request: Request,
    *,
    source_id: int | None,
    source_name: str | None = None,
) -> Response:
    base = state.base_url(request)
    episodes = collect_episodes(
        state.context.entries,
        base_url=base,
        source_id=source_id,
        audio_base_url=state.audio_base_url(request),
        token=state.feed_token,
    )
    title = f"vocast — {source_name}" if source_name else "vocast"
    description = (
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
            "worker": "running" if state.worker_running else "stopped",
            "poller": "running" if state.poller_running else "stopped",
            "sources": source_count,
            "entries": counts,
            "pending": counts.get(EntryStatus.PENDING.value, 0),
            "failed": counts.get(EntryStatus.FAILED.value, 0),
            "last_successful_poll": last_poll.isoformat() if last_poll else None,
        }
        return JSONResponse(payload, status_code=200 if database_ok else 503)


# --- admin API -------------------------------------------------------------


def _register_admin(router: APIRouter, state: ServiceState) -> None:
    require_admin = _admin_guard(state)

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
