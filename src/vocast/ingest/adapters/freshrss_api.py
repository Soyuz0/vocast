"""FreshRSS via the Google Reader compatible API.

Why this exists alongside the feed adapter: a FreshRSS RSS document is a capped
window (20 items by default, and its `page` parameter is ignored for RSS
output), so it cannot enumerate a large unread backlog. The Google Reader API
can, through `continuation` cursors.

Stream ordering is strictly descending by *crawl* time -- when FreshRSS fetched
the article -- not by publication date. That is what makes pagination reliable,
since crawl time is monotonic across pages while publication dates are mixed
within a page. Publication dates are still recorded on every entry, so the
worker can narrate in true newest-published-first order.

Authentication is ClientLogin: POST the username and the FreshRSS *API
password* (Settings > Profile > API management, distinct from the login
password) and get back a token used as `Authorization: GoogleLogin auth=...`.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from ..models import FeedEntry, Source
from ..nethttp import FetchError, FetchPolicy, fetch
from .base import FeedParseError

#: Google Reader stream and state identifiers.
READING_LIST = "user/-/state/com.google/reading-list"
READ_STATE = "user/-/state/com.google/read"
STARRED_STATE = "user/-/state/com.google/starred"

DEFAULT_PAGE_SIZE = 200
#: FreshRSS rejects larger values outright.
MAX_PAGE_SIZE = 1000
#: Safety rail on a single poll, so a runaway continuation cannot loop forever.
MAX_PAGES = 500

KnownGuids = Callable[[Iterable[str]], set[str]]


class FreshRSSAPIAdapter:
    """Enumerates articles from FreshRSS through its Google Reader API.

    `Source.url` is the FreshRSS *base* URL (e.g. `https://freshrss.example.com`),
    not a feed URL.

    Per-source `config` keys:

    * `username`      — FreshRSS account name (required)
    * `api_password`  — the API password (required)
    * `unread_only`   — only unread articles; default true
    * `starred_only`  — only starred articles; default false
    * `stream`        — override the stream id (default: the reading list)
    * `page_size`     — items per request, capped at 1000; default 200
    * `max_entries_per_poll` — stop after this many articles
    * `allow_private_urls`, `timeout_seconds`, `max_bytes` — fetch policy
    """

    def __init__(
        self,
        source: Source,
        *,
        policy: FetchPolicy | None = None,
        fetcher: Callable[..., Any] = fetch,
        known_guids: KnownGuids | None = None,
    ) -> None:
        self._source = source
        self._fetcher = fetcher
        self._known_guids = known_guids
        config = source.config or {}
        base = policy or FetchPolicy()
        self._policy = FetchPolicy(
            timeout=float(config.get("timeout_seconds", 60.0)),
            max_bytes=int(config.get("max_bytes", 64 * 1024 * 1024)),
            allow_private=bool(config.get("allow_private_urls", base.allow_private)),
            user_agent=str(config.get("user_agent", base.user_agent)),
        )
        self._auth_token: str | None = None

    @property
    def source(self) -> Source:
        return self._source

    def fetch_entries(self) -> list[FeedEntry]:
        config = self._source.config or {}
        username = config.get("username")
        password = config.get("api_password") or config.get("password")
        if not username or not password:
            raise FeedParseError(
                f"source {self._source.id} ({self._source.name}) needs `username` and "
                "`api_password` to use the FreshRSS API; the API password is set in "
                "FreshRSS under Settings > Profile and is not the login password"
            )

        token = self._client_login(str(username), str(password))
        return self._collect(token)

    # -- authentication ----------------------------------------------------

    def _client_login(self, username: str, password: str) -> str:
        url = f"{self._base()}/api/greader.php/accounts/ClientLogin"
        body = urllib.parse.urlencode({"Email": username, "Passwd": password})
        try:
            response = self._fetcher(
                url,
                policy=self._policy,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=body.encode("utf-8"),
            )
        except FetchError as exc:
            # Never echo the password back, even indirectly.
            raise FeedParseError(f"FreshRSS ClientLogin failed: {exc}") from exc

        for line in response.text().splitlines():
            if line.startswith("Auth="):
                return line[len("Auth=") :].strip()
        raise FeedParseError(
            "FreshRSS ClientLogin returned no Auth token; check the username and API "
            "password, and that the API is enabled in FreshRSS"
        )

    # -- pagination --------------------------------------------------------

    def _collect(self, token: str) -> list[FeedEntry]:
        config = self._source.config or {}
        page_size = min(
            max(1, int(config.get("page_size", DEFAULT_PAGE_SIZE))), MAX_PAGE_SIZE
        )
        limit = config.get("max_entries_per_poll")
        max_entries = int(limit) if limit else None

        entries: list[FeedEntry] = []
        seen: set[str] = set()
        continuation: str | None = None

        for _ in range(MAX_PAGES):
            payload = self._request_page(token, page_size, continuation)
            items = payload.get("items") or []
            if not items:
                break

            page_entries = [e for e in map(self._to_entry, items) if e is not None]
            fresh = self._count_unknown(page_entries)
            for entry in page_entries:
                if entry.external_guid in seen:
                    continue
                seen.add(entry.external_guid)
                entries.append(entry)

            if max_entries is not None and len(entries) >= max_entries:
                return entries[:max_entries]

            # Steady state: the stream is newest-crawled first, so once a whole
            # page is already known, everything deeper is too. Without this,
            # every poll would re-download the entire backlog.
            if fresh == 0 and self._known_guids is not None:
                break

            continuation = payload.get("continuation")
            if not continuation:
                break

        return entries

    def _count_unknown(self, entries: list[FeedEntry]) -> int:
        if self._known_guids is None or not entries:
            return len(entries)
        guids = [e.external_guid for e in entries]
        return len(guids) - len(self._known_guids(guids))

    def _request_page(
        self, token: str, page_size: int, continuation: str | None
    ) -> dict[str, Any]:
        config = self._source.config or {}
        stream = str(config.get("stream", READING_LIST))
        params: list[tuple[str, str]] = [
            ("n", str(page_size)),
            ("output", "json"),
        ]
        if config.get("unread_only", True):
            params.append(("xt", READ_STATE))
        if config.get("starred_only", False):
            params.append(("s", STARRED_STATE))
        if continuation:
            params.append(("c", continuation))

        url = (
            f"{self._base()}/api/greader.php/reader/api/0/stream/contents/"
            f"{urllib.parse.quote(stream, safe='/-')}?{urllib.parse.urlencode(params)}"
        )
        try:
            response = self._fetcher(
                url,
                policy=self._policy,
                headers={"Authorization": f"GoogleLogin auth={token}"},
            )
        except FetchError as exc:
            raise FeedParseError(f"FreshRSS API request failed: {exc}") from exc

        try:
            payload = json.loads(response.text())
        except json.JSONDecodeError as exc:
            raise FeedParseError(
                f"FreshRSS API returned invalid JSON from {self._source.url}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise FeedParseError("FreshRSS API returned an unexpected payload shape")
        return payload

    # -- mapping -----------------------------------------------------------

    def _to_entry(self, item: Any) -> FeedEntry | None:
        if not isinstance(item, dict):
            return None
        article_url = _alternate_href(item)
        if not article_url:
            return None
        guid = item.get("id")
        if not isinstance(guid, str) or not guid:
            return None
        return FeedEntry(
            source_id=self._source.id,
            external_guid=guid,
            title=_clean(item.get("title")) or "untitled",
            article_url=article_url,
            published_at=_published(item),
            author=_clean(item.get("author")),
            summary=_summary(item),
        )

    def _base(self) -> str:
        return self._source.url.rstrip("/")


def _alternate_href(item: dict[str, Any]) -> str | None:
    """The publisher's URL, so the article is narrated rather than FreshRSS."""
    for link in item.get("alternate") or []:
        if isinstance(link, dict):
            href = link.get("href")
            if isinstance(href, str) and href.startswith(("http://", "https://")):
                return href
    canonical = item.get("canonical")
    if isinstance(canonical, list):
        for link in canonical:
            if isinstance(link, dict):
                href = link.get("href")
                if isinstance(href, str) and href.startswith(("http://", "https://")):
                    return href
    return None


def _published(item: dict[str, Any]) -> datetime | None:
    for key in ("published", "updated"):
        value = item.get(key)
        if value in (None, "", 0):
            continue
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    return None


def _summary(item: dict[str, Any]) -> str | None:
    for key in ("summary", "content"):
        block = item.get(key)
        if isinstance(block, dict):
            text = block.get("content")
            if isinstance(text, str) and text.strip():
                # Kept short: this only ever becomes feed show-notes.
                return text.strip()[:2000]
    return None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
