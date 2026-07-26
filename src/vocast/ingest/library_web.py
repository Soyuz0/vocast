"""Server-rendered searchable library."""

from __future__ import annotations

import secrets
from datetime import date, datetime, time, timezone
from functools import lru_cache
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, PackageLoader, select_autoescape

from .api import ServiceState
from .library_query import LibraryQuery, LibraryQueryService
from .models import EntryStatus

_LIBRARY_TOKEN_COOKIE = "vocast_library_token"


@lru_cache(maxsize=1)
def _templates() -> Environment:
    return Environment(
        loader=PackageLoader("vocast", "templates"),
        autoescape=select_autoescape(("html", "xml")),
    )


def register_library(router: APIRouter, state: ServiceState) -> None:
    @router.get("/public/library", response_class=HTMLResponse, include_in_schema=False)
    @router.get("/library", response_class=HTMLResponse)
    def library_page(
        request: Request,
        token: str | None = None,
        search: str | None = None,
        source_id: int | None = None,
        origin_id: str | None = None,
        status: str | None = None,
        queued: str | None = None,
        downloaded: str | None = None,
        published_after: str | None = None,
        published_before: str | None = None,
        min_duration_seconds: int | None = None,
        max_duration_seconds: int | None = None,
        sort: str = "published_desc",
        page: int = 1,
        page_size: int = 50,
    ) -> Response:
        if request.url.path.endswith("/public/library"):
            redirect = _require_library_token(state, request, token)
            if redirect is not None:
                return redirect
        query = LibraryQuery(
            search=search,
            source_id=source_id,
            origin_id=origin_id,
            status=_status(status),
            queued=_boolean(queued, "queued"),
            downloaded=_boolean(downloaded, "downloaded"),
            published_after=_date_boundary(published_after, end=False),
            published_before=_date_boundary(published_before, end=True),
            min_duration_seconds=min_duration_seconds,
            max_duration_seconds=max_duration_seconds,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        service = LibraryQueryService(state.context.db)
        result = service.search(query)
        current_query = {
            key: value
            for key, value in request.query_params.items()
            if key not in ("page", "token") and value != ""
        }
        html = _templates().get_template("library.html").render(
            page=result,
            sources=service.sources(),
            origins=service.origins(),
            statuses=list(EntryStatus),
            current_query=current_query,
            pagination_query=urlencode(current_query),
            admin_token_required=bool(state.context.config.admin_token),
            feed_token_required=bool(state.context.config.server.feed_token),
            base_path=urlsplit(state.base_url(request)).path.rstrip("/"),
        )
        return HTMLResponse(html)


def _require_library_token(
    state: ServiceState, request: Request, supplied: str | None
) -> Response | None:
    expected = state.context.config.server.feed_token
    if not expected:
        return None
    cookie = request.cookies.get(_LIBRARY_TOKEN_COOKIE)
    if cookie and secrets.compare_digest(cookie, expected):
        return None
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "a valid ?token= is required for the library")

    base = state.base_url(request)
    base_path = urlsplit(base).path.rstrip("/")
    query_values = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != "token"
    ]
    query = urlencode(query_values)
    location = f"{base_path}/library"
    if query:
        location = f"{location}?{query}"
    response = RedirectResponse(location, status_code=303)
    response.set_cookie(
        _LIBRARY_TOKEN_COOKIE,
        expected,
        httponly=True,
        secure=urlsplit(base).scheme == "https",
        samesite="strict",
        path=base_path or "/",
    )
    return response


def _status(value: str | None) -> EntryStatus | None:
    if not value:
        return None
    try:
        return EntryStatus(value)
    except ValueError as exc:
        raise HTTPException(400, f"unknown status {value!r}") from exc


def _boolean(value: str | None, name: str) -> bool | None:
    if not value:
        return None
    if value == "yes":
        return True
    if value == "no":
        return False
    raise HTTPException(400, f"{name} must be yes or no")


def _date_boundary(value: str | None, *, end: bool) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"invalid date {value!r}") from exc
    return datetime.combine(parsed, time.max if end else time.min, timezone.utc)
