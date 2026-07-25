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
        self._feed_icons: dict[str, str] = {}
        self._feed_sites: dict[str, str] = {}
        #: True only when the last fetch reached the end of the stream. Anything
        #: that reconciles "what is still unread upstream" must check this: from
        #: a partial walk, absence proves nothing.
        self.walk_complete = False

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
        self._feed_icons = self._load_feed_icons(token)
        self.walk_complete = False
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

    # -- per-feed artwork --------------------------------------------------

    def _load_feed_icons(self, token: str) -> dict[str, str]:
        """Map each subscription's stream id to its icon URL.

        One request covers every feed, so episode artwork costs nothing per
        article. Failure is tolerated: artwork is cosmetic and must never stop
        articles being discovered.
        """
        if not self._source.config.get("use_feed_icons", True):
            return {}
        url = (
            f"{self._base()}/api/greader.php/reader/api/0/subscription/list?output=json"
        )
        try:
            response = self._fetcher(
                url,
                policy=self._policy,
                headers={"Authorization": f"GoogleLogin auth={token}"},
            )
            payload = json.loads(response.text())
        except (FetchError, json.JSONDecodeError, ValueError):
            return {}

        icons: dict[str, str] = {}
        for sub in payload.get("subscriptions") or []:
            if not isinstance(sub, dict):
                continue
            stream_id, icon = sub.get("id"), sub.get("iconUrl")
            if isinstance(stream_id, str) and isinstance(icon, str) and icon:
                icons[stream_id] = self._localize(icon)
            site = sub.get("htmlUrl")
            if isinstance(stream_id, str) and isinstance(site, str) and site:
                self._feed_sites[stream_id] = site
        return icons

    def _localize(self, url: str) -> str:
        """Rewrite a FreshRSS-reported URL onto the host we actually reach it on.

        FreshRSS builds these from its own configured base URL, which is often
        something like http://127.0.0.1:8082 -- and from another container that
        resolves to the wrong machine entirely.
        """
        try:
            reported = urllib.parse.urlsplit(url)
            ours = urllib.parse.urlsplit(self._base())
        except ValueError:
            return url
        if not reported.netloc or reported.netloc == ours.netloc:
            return url
        return urllib.parse.urlunsplit(
            (
                ours.scheme or reported.scheme,
                ours.netloc,
                reported.path,
                reported.query,
                reported.fragment,
            )
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
                self.walk_complete = True
                break

            page_entries = [e for e in map(self._to_entry, items) if e is not None]
            fresh = self._count_unknown(page_entries)
            for entry in page_entries:
                if entry.external_guid in seen:
                    continue
                seen.add(entry.external_guid)
                entries.append(entry)

            if max_entries is not None and len(entries) >= max_entries:
                # Truncated by the cap, so the stream was not exhausted.
                return entries[:max_entries]

            # Steady state: the stream is newest-crawled first, so once a whole
            # page is already known, everything deeper is too. Without this,
            # every poll would re-download the entire backlog.
            if fresh == 0 and self._known_guids is not None:
                # Stopped early on purpose; deeper pages were never seen.
                break

            continuation = payload.get("continuation")
            if not continuation:
                self.walk_complete = True
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
            origin_name=_origin_name(item),
            origin_image_url=self._icon_for(item),
            feed_content=self._own_text(item, article_url),
        )

    def _own_text(self, item: dict[str, Any], article_url: str) -> str | None:
        """The post's text, when the link points away from the publication.

        Link-blog posts link outward to whatever is being discussed, so
        following the link narrates someone else's article rather than the post
        itself -- and fails outright when that target is paywalled. When the
        link leaves the feed's own site and the entry carries a substantial body
        of its own, that body is the article.

        Also catches publications served from two domains (a custom domain with
        the platform's own URL in the link). Those are not link posts, but their
        feed carries the full text, so preferring it is still correct.
        """
        config = self._source.config or {}
        if not config.get("prefer_own_text", True):
            return None
        site = self._feed_sites.get(
            (item.get("origin") or {}).get("streamId", "")
            if isinstance(item.get("origin"), dict)
            else ""
        )
        if not site or _same_site(site, article_url):
            return None
        body = _body_html(item)
        if not body:
            return None
        minimum = int(config.get("min_own_text_chars", 400))
        return body if len(_visible_length(body)) >= minimum else None

    def _icon_for(self, item: dict[str, Any]) -> str | None:
        origin = item.get("origin")
        if not isinstance(origin, dict):
            return None
        stream_id = origin.get("streamId")
        if not isinstance(stream_id, str):
            return None
        return getattr(self, "_feed_icons", {}).get(stream_id)

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


def _same_site(site: str, article_url: str) -> bool:
    def host(url: str) -> str:
        parsed = urllib.parse.urlsplit(url).hostname or ""
        return parsed.removeprefix("www.").lower()

    return host(site) == host(article_url)


def _body_html(item: dict[str, Any]) -> str | None:
    for key in ("content", "summary"):
        block = item.get(key)
        if isinstance(block, dict):
            text = block.get("content")
            if isinstance(text, str) and text.strip():
                return text
    return None


def _visible_length(html_fragment: str) -> str:
    import html as html_module
    import re

    stripped = re.sub(r"<[^>]+>", " ", html_fragment)
    return re.sub(r"\s+", " ", html_module.unescape(stripped)).strip()


def _origin_name(item: dict[str, Any]) -> str | None:
    """The upstream feed's title, e.g. the publication name.

    FreshRSS aggregates many feeds into one stream, so this is what identifies
    an article's actual publisher.
    """
    origin = item.get("origin")
    if isinstance(origin, dict):
        return _clean(origin.get("title"))
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
