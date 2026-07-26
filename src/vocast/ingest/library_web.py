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

#: Length filters, as (label, min seconds, max seconds). Chosen around how long
#: a listening slot is rather than round numbers.
DURATION_BUCKETS = (
    ("Under 10 min", None, 600),
    ("10-30 min", 600, 1800),
    ("30-60 min", 1800, 3600),
    ("Over 1 hour", 3600, None),
)


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
        source_id: str | None = None,
        origin_id: str | None = None,
        status: str | None = None,
        queued: str | None = None,
        downloaded: str | None = None,
        published_after: str | None = None,
        published_before: str | None = None,
        min_duration_seconds: str | None = None,
        max_duration_seconds: str | None = None,
        sort: str = "published_desc",
        page: str | None = None,
        page_size: str | None = None,
    ) -> Response:
        # Internet requests are already rejected by the app-wide guard, which
        # covers every path because Funnel publishes every path. This handles
        # the remaining case: turning a ?token= into a cookie so the browser
        # keeps working as you navigate.
        if token or request.url.path.endswith("/public/library"):
            redirect = _require_library_token(state, request, token)
            if redirect is not None:
                return redirect
        query = LibraryQuery(
            search=search,
            source_id=_optional_int(source_id, "source_id"),
            origin_id=origin_id,
            status=_status(status),
            queued=_boolean(queued, "queued"),
            downloaded=_boolean(downloaded, "downloaded"),
            published_after=_date_boundary(published_after, end=False),
            published_before=_date_boundary(published_before, end=True),
            min_duration_seconds=_optional_int(
                min_duration_seconds, "min_duration_seconds"
            ),
            max_duration_seconds=_optional_int(
                max_duration_seconds, "max_duration_seconds"
            ),
            sort=sort,
            page=_optional_int(page, "page") or 1,
            page_size=_optional_int(page_size, "page_size") or 50,
        )
        service = LibraryQueryService(state.context.db)
        result = service.search(query)
        base_path = urlsplit(state.base_url(request)).path.rstrip("/")
        current_query = {
            key: value
            for key, value in request.query_params.items()
            if key not in ("page", "token") and value != ""
        }
        html = (
            _templates()
            .get_template("library.html")
            .render(
                page=result,
                sources=service.sources(),
                facets=service.facets(),
                statuses=list(EntryStatus),
                current_query=current_query,
                pagination_query=urlencode(current_query),
                feed_token=state.feed_token or "",
                base_path=base_path,
                duration_buckets=DURATION_BUCKETS,
                link=_link_builder(base_path, current_query),
            )
        )
        return HTMLResponse(html)


def _is_secure(request: Request) -> bool:
    """Whether the client's own connection is encrypted.

    Behind a TLS-terminating proxy the request arrives as HTTP, so the
    forwarded protocol header is authoritative when present.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


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
        # Derived from how the client actually connected, not from the
        # configured public URL. Those differ: the tailnet address is plain
        # HTTP while public_base_url is HTTPS, and a Secure cookie would then
        # never be sent back, leaving the page redirecting to itself forever.
        secure=_is_secure(request),
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


def _link_builder(base_path: str, current: dict[str, str]):
    """Return a helper that builds library URLs from the active filters.

    Templates need "this page, but with one filter changed or cleared", which
    Jinja cannot express: its macros take no **kwargs. Building the query string
    in Python also keeps escaping in one place.
    """

    def link(**overrides: object) -> str:
        merged = dict(current)
        for key, value in overrides.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = str(value)
        query = urlencode(sorted(merged.items()))
        return f"{base_path}/library?{query}" if query else f"{base_path}/library"

    return link


def _optional_int(value: str | None, name: str) -> int | None:
    """Parse a numeric filter, treating blank as absent.

    Submitting the filter form sends every field, so an untouched number or
    select arrives as an empty string. Declaring these as int would make the
    whole page fail validation rather than simply not filtering on them.
    """
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(400, f"{name} must be a whole number") from None


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
