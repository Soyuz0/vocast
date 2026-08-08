"""A phone-sized reader for the library, modelled on Reeder's two columns.

Deliberately separate from /library rather than another breakpoint of it. The
desktop page is a workbench -- every facet, a player, bulk actions -- and the
phone wants the opposite: a list of places to go, then a dense list of articles
you can flick through one-handed. Trying to be both in one template is what
produced the breakpoint-specific duplicate controls already in library.html.

There is no player here on purpose. This surface is for triage -- read, star,
open the original -- and listening happens in a podcast client subscribed to
the feed.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from .api import ServiceState
from .library_query import LibraryQuery, LibraryQueryService
from .library_web import require_page_token, templates
from .models import EntryStatus

MOBILE_PATH = "/m"
ARTICLES_PATH = "/m/articles"

LISTEN_LATER = "listen-later"

#: Segment order in the toolbar. Unread is the default because a reader that
#: opens on everything you have already read is a search tool, not an inbox.
FILTERS = ("unread", "read", "all")

_FILTER_READ_STATE: dict[str, bool | None] = {
    "unread": False,
    "read": True,
    "all": None,
}
_FILTER_LABEL = {"unread": "Unread", "read": "Read", "all": "All"}

#: Always listed, even at zero: "0 failed" is worth knowing, and a section that
#: changed length as the pipeline moved would shift the publications under a
#: thumb already reaching for them.
STANDING_STATUSES = (
    EntryStatus.READY,
    EntryStatus.PROCESSING,
    EntryStatus.PENDING,
    EntryStatus.FAILED,
)
#: Listed only when they have something in them. Both are end states nobody is
#: waiting on, so an empty row would be pure furniture.
OCCASIONAL_STATUSES = (EntryStatus.IGNORED, EntryStatus.EXPIRED)


@dataclass(frozen=True)
class MobileFilter:
    name: str
    label: str
    url: str
    active: bool


@dataclass(frozen=True)
class MobileStatus:
    value: str
    label: str
    count: int


def register_mobile(router: APIRouter, state: ServiceState) -> None:
    @router.get(MOBILE_PATH, response_class=HTMLResponse, include_in_schema=False)
    def mobile_sources(
        request: Request,
        token: str | None = None,
        filter: str = "unread",
    ) -> Response:
        landed = _land_with_token(state, request, token, MOBILE_PATH)
        if landed is not None:
            return landed
        state.sync_read_state()

        active_filter = _filter_name(filter)
        read = _FILTER_READ_STATE[active_filter]
        service = LibraryQueryService(state.context.db)
        base_path = _base_path(state, request)

        # One query object behind every number on the page, so no count can
        # disagree with the filter the toolbar says is active.
        everything = LibraryQuery(read=read)
        articles_url = f"{base_path}{ARTICLES_PATH}"
        html = _render(
            "mobile_sources.html",
            base_path=base_path,
            active_filter=active_filter,
            filter_label=_FILTER_LABEL[active_filter],
            filters=[
                MobileFilter(
                    name=name,
                    label=_FILTER_LABEL[name],
                    url=_url(base_path, MOBILE_PATH, filter=name),
                    active=name == active_filter,
                )
                for name in FILTERS
            ],
            self_url=_url(base_path, MOBILE_PATH, filter=active_filter),
            search_action=articles_url,
            search_hidden={"filter": active_filter},
            search_clear_url=_url(base_path, ARTICLES_PATH, filter=active_filter),
            search_value="",
            library_count=service.count(everything),
            listen_later_count=service.count(LibraryQuery(read=read, queued=True)),
            statuses=_status_rows(service.status_counts(everything)),
            publications=service.facets(everything).origins,
            destination=lambda **params: _url(
                base_path, ARTICLES_PATH, filter=active_filter, **params
            ),
        )
        return HTMLResponse(html)

    @router.get(ARTICLES_PATH, response_class=HTMLResponse, include_in_schema=False)
    def mobile_articles(
        request: Request,
        token: str | None = None,
        filter: str = "unread",
        origin_id: str | None = None,
        playlist: str | None = None,
        status: str | None = None,
        search: str | None = None,
        page: str | None = None,
    ) -> Response:
        landed = _land_with_token(state, request, token, ARTICLES_PATH)
        if landed is not None:
            return landed
        state.sync_read_state()

        active_filter = _filter_name(filter)
        selected_status = _entry_status(status)
        queued = True if playlist == LISTEN_LATER else None
        origin_id = (origin_id or "").strip() or None
        search = (search or "").strip() or None
        service = LibraryQueryService(state.context.db)
        result = service.search(
            LibraryQuery(
                search=search,
                origin_id=origin_id,
                queued=queued,
                status=selected_status,
                read=_FILTER_READ_STATE[active_filter],
                page=_page_number(page),
                page_size=50,
            )
        )
        base_path = _base_path(state, request)
        # Everything that says *which* articles these are, as opposed to how
        # they are filtered or paged. Carried by search, paging and the filter
        # segments alike, so none of them can quietly widen the selection.
        selection = {
            key: value
            for key, value in (
                ("origin_id", origin_id),
                ("playlist", playlist if queued else None),
                ("status", selected_status.value if selected_status else None),
            )
            if value
        }
        current = dict(selection, search=search) if search else dict(selection)
        html = _render(
            "mobile_articles.html",
            base_path=base_path,
            title=_selection_title(service, origin_id, playlist, selected_status),
            page=result,
            # Only a publication has a feed of its own to subscribe to, so the
            # copy control appears for one and not for Library or Listen Later.
            selected_origin_id=origin_id,
            active_filter=active_filter,
            filter_label=_FILTER_LABEL[active_filter],
            filters=[
                MobileFilter(
                    name=name,
                    label=_FILTER_LABEL[name],
                    url=_url(base_path, ARTICLES_PATH, filter=name, **current),
                    active=name == active_filter,
                )
                for name in FILTERS
            ],
            self_url=_url(
                base_path,
                ARTICLES_PATH,
                filter=active_filter,
                page=result.query.page if result.query.page > 1 else None,
                **current,
            ),
            back_url=_url(base_path, MOBILE_PATH, filter=active_filter),
            search_action=f"{base_path}{ARTICLES_PATH}",
            search_hidden={"filter": active_filter, **selection},
            search_clear_url=_url(
                base_path, ARTICLES_PATH, filter=active_filter, **selection
            ),
            search_value=search or "",
            page_url=lambda number: _url(
                base_path,
                ARTICLES_PATH,
                filter=active_filter,
                page=number,
                **current,
            ),
        )
        return HTMLResponse(html)


def _render(template: str, **values: object) -> str:
    # No feed token among the values, and none reachable from them. The page is
    # unauthenticated on the tailnet, so anything embedded here is readable by
    # anyone who can reach the host; the public feed's secret must not be.
    return templates().get_template(template).render(**values)


def _land_with_token(
    state: ServiceState, request: Request, token: str | None, page_path: str
) -> Response | None:
    """Swap a ?token= for the cookie the rest of the navigation relies on.

    Links between the two pages carry no token, so arriving over the internet
    with one in the URL has to leave something behind or the first tap would be
    a 401.
    """
    if not token:
        return None
    return require_page_token(state, request, token, page_path=page_path)


def _status_rows(counts: dict[str, int]) -> list[MobileStatus]:
    rows = [
        MobileStatus(
            value=status.value,
            label=status.value.capitalize(),
            count=counts.get(status.value, 0),
        )
        for status in STANDING_STATUSES
    ]
    rows.extend(
        MobileStatus(
            value=status.value,
            label=status.value.capitalize(),
            count=counts[status.value],
        )
        for status in OCCASIONAL_STATUSES
        if counts.get(status.value)
    )
    return rows


def _selection_title(
    service: LibraryQueryService,
    origin_id: str | None,
    playlist: str | None,
    status: EntryStatus | None,
) -> str:
    """What the header calls this list, narrowest selection first."""
    if playlist == LISTEN_LATER:
        return "Listen Later"
    if origin_id:
        folded = origin_id.casefold()
        for origin in service.origins():
            if origin.id == folded:
                return origin.name
        return origin_id
    if status is not None:
        return status.value.capitalize()
    return "Library"


def _base_path(state: ServiceState, request: Request) -> str:
    return urlsplit(state.base_url(request)).path.rstrip("/")


def _url(base_path: str, path: str, **params: object) -> str:
    query = urlencode(
        sorted(
            (key, str(value))
            for key, value in params.items()
            if value is not None and value != ""
        )
    )
    return f"{base_path}{path}?{query}" if query else f"{base_path}{path}"


def _filter_name(value: str | None) -> str:
    """Fall back rather than reject: a bad ?filter= is not worth an error page."""
    return value if value in FILTERS else "unread"


def _entry_status(value: str | None) -> EntryStatus | None:
    """Same leniency as the read filter: an unrecognised status is dropped.

    The desktop page answers 400 here, which suits a page you arrive at from
    its own controls. This one is bookmarked and shared to a phone, where a
    status renamed since the link was saved should still show a library.
    """
    if not value:
        return None
    try:
        return EntryStatus(value)
    except ValueError:
        return None


def _page_number(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except ValueError:
        return 1
