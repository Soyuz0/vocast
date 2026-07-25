"""RSS 2.0 / Atom adapter built on feedparser."""

from __future__ import annotations

import calendar
import hashlib
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import feedparser

from ..models import FeedEntry, Source
from ..nethttp import FetchPolicy, basic_auth_header, fetch
from .base import FeedParseError

Fetcher = Callable[..., Any]

DEFAULT_MAX_ENTRIES_PER_POLL = 50

_FEED_ACCEPT = (
    "application/atom+xml, application/rss+xml, application/xml;q=0.9, text/xml;q=0.8"
)


class GenericRSSAdapter:
    """Reads any RSS or Atom feed reachable over HTTP.

    Per-source `config` keys:

    * `headers`      — extra request headers (e.g. a custom User-Agent)
    * `username`     — HTTP Basic Auth user, paired with `password`
    * `password`     — HTTP Basic Auth password
    * `allow_private_urls` — permit LAN/loopback hosts for this source
    * `max_entries_per_poll` — cap on articles accepted from one response
    * `timeout_seconds`, `max_bytes` — override the fetch policy
    """

    def __init__(
        self,
        source: Source,
        *,
        policy: FetchPolicy | None = None,
        fetcher: Fetcher = fetch,
        known_guids: Callable[..., Any] | None = None,
    ) -> None:
        self._source = source
        self._fetcher = fetcher
        self._policy = _policy_for(source, policy)
        # Accepted for a uniform adapter signature but unused: a feed document
        # is a single response, so there is no deeper pagination to skip.
        self._known_guids = known_guids

    @property
    def source(self) -> Source:
        return self._source

    def fetch_entries(self) -> list[FeedEntry]:
        raw = self._download()
        return self._parse(raw)

    # -- internals ---------------------------------------------------------

    def _download(self) -> bytes:
        response = self._fetcher(
            self._source.url,
            policy=self._policy,
            headers=self._request_headers(),
            accept=_FEED_ACCEPT,
        )
        return response.body

    def _request_headers(self) -> dict[str, str]:
        config = self._source.config or {}
        headers: dict[str, str] = {}
        configured = config.get("headers")
        if isinstance(configured, dict):
            headers.update({str(k): str(v) for k, v in configured.items()})

        username = config.get("username")
        password = config.get("password")
        # An explicit Authorization header wins, so a user can supply a
        # pre-encoded token without also inventing a username/password pair.
        if username and password and not _has_header(headers, "authorization"):
            headers["Authorization"] = basic_auth_header(str(username), str(password))
        return headers

    def _parse(self, raw: bytes) -> list[FeedEntry]:
        parsed = feedparser.parse(raw)
        items = parsed.get("entries") or []

        # feedparser sets `bozo` for anything from a stray ampersand to
        # complete garbage, and still returns entries in the former case.
        # Only give up when nothing usable came back.
        if not items and parsed.get("bozo"):
            reason = parsed.get("bozo_exception") or "unknown parse error"
            raise FeedParseError(
                f"could not parse feed at {self._source.url}: {reason}"
            )

        base = _feed_base_url(parsed, self._source.url)
        limit = _max_entries(self._source)

        results: list[FeedEntry] = []
        seen: set[str] = set()
        for item in items:
            entry = self._to_feed_entry(item, base)
            if entry is None or entry.external_guid in seen:
                continue
            seen.add(entry.external_guid)
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    def _to_feed_entry(self, item: Any, base: str) -> FeedEntry | None:
        article_url = _absolute_link(item, base)
        if not article_url:
            # Without a URL there is nothing to extract, so the item is
            # unusable rather than merely incomplete.
            return None
        return FeedEntry(
            source_id=self._source.id,
            external_guid=_entry_guid(item, article_url),
            title=_entry_title(item),
            article_url=article_url,
            published_at=_entry_published(item),
            author=_first_string(item, "author"),
            summary=_first_string(item, "summary", "description"),
        )


def _policy_for(source: Source, override: FetchPolicy | None) -> FetchPolicy:
    base = override or FetchPolicy()
    config = source.config or {}
    return FetchPolicy(
        timeout=float(config.get("timeout_seconds", base.timeout)),
        max_bytes=int(config.get("max_bytes", base.max_bytes)),
        allow_private=bool(config.get("allow_private_urls", base.allow_private)),
        user_agent=str(config.get("user_agent", base.user_agent)),
    )


def _max_entries(source: Source) -> int:
    config = source.config or {}
    try:
        value = int(config.get("max_entries_per_poll", DEFAULT_MAX_ENTRIES_PER_POLL))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ENTRIES_PER_POLL
    return max(1, value)


def _has_header(headers: dict[str, str], name: str) -> bool:
    return any(key.lower() == name for key in headers)


def _feed_base_url(parsed: Any, fallback: str) -> str:
    """Best base for resolving relative links: xml:base, then the feed's own
    link, then the URL we fetched."""
    for candidate in (
        (parsed.get("feed") or {}).get("link"),
        parsed.get("href"),
    ):
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            return candidate
    return fallback


def _absolute_link(item: Any, base: str) -> str | None:
    candidates: list[str] = []
    link = item.get("link")
    if isinstance(link, str):
        candidates.append(link)
    for link_info in item.get("links") or []:
        href = link_info.get("href") if isinstance(link_info, dict) else None
        if isinstance(href, str) and link_info.get("rel", "alternate") == "alternate":
            candidates.append(href)

    for candidate in candidates:
        resolved = urllib.parse.urljoin(base, candidate.strip())
        if resolved.startswith(("http://", "https://")):
            return resolved
    return None


def _entry_guid(item: Any, article_url: str) -> str:
    """Prefer the feed's own stable identity, else fall back to the URL.

    The final fallback hashes title and date, which keeps identity stable for
    feeds that supply neither an id nor a usable link.
    """
    for key in ("id", "guid"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if article_url:
        return article_url
    digest = hashlib.sha256(
        f"{_entry_title(item)}|{item.get('published', '')}".encode()
    ).hexdigest()
    return f"sha256:{digest}"


def _entry_title(item: Any) -> str:
    title = _first_string(item, "title")
    return title or "untitled"


def _first_string(item: Any, *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _entry_published(item: Any) -> datetime | None:
    """Normalize whichever date field the feed happens to provide, to UTC.

    Returns None when no date is parseable; the poller treats discovery time
    as the effective date in that case.
    """
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = item.get(key)
        if struct is None:
            continue
        try:
            return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
    return None
