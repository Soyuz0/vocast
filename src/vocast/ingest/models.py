"""Domain types for ingestion state."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .timeutils import from_iso


class EntryStatus(str, Enum):
    """Lifecycle of a discovered article.

    pending    -> discovered, waiting for a worker
    processing -> claimed by a worker
    ready      -> an episode exists and is published in the feed
    failed     -> retries exhausted; needs manual attention
    ignored    -> deliberately skipped, never to be generated
    expired    -> episode was generated then removed by retention

    `expired` is kept distinct from a deleted row on purpose: the row remains
    as the deduplication guard, so retention cannot cause an article to be
    rediscovered and re-synthesized on the next poll.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    IGNORED = "ignored"
    EXPIRED = "expired"


class SourceKind(str, Enum):
    RSS = "rss"
    FRESHRSS_FEED = "freshrss_feed"
    FRESHRSS_API = "freshrss_api"


@dataclass(frozen=True)
class FeedEntry:
    """An article as a source adapter reports it, before it is persisted."""

    source_id: int
    external_guid: str
    title: str
    article_url: str
    published_at: datetime | None
    author: str | None = None
    summary: str | None = None
    #: Name of the upstream feed that carried the article, e.g. a publication
    #: name. Distinct from the vocast source, which may aggregate many feeds.
    origin_name: str | None = None
    #: Artwork for that publication, used as the episode's cover.
    origin_image_url: str | None = None


@dataclass(frozen=True)
class Source:
    id: int
    name: str
    kind: str
    url: str
    enabled: bool
    poll_interval_minutes: int
    config: dict[str, Any]
    last_checked_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Source:
        return cls(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            url=row["url"],
            enabled=bool(row["enabled"]),
            poll_interval_minutes=row["poll_interval_minutes"],
            config=_decode_config(row["config_json"]),
            last_checked_at=from_iso(row["last_checked_at"]),
            last_success_at=from_iso(row["last_success_at"]),
            last_error=row["last_error"],
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
        )


@dataclass(frozen=True)
class Entry:
    id: int
    source_id: int
    external_guid: str
    article_url: str
    title: str
    author: str | None
    published_at: datetime | None
    origin_name: str | None
    origin_image_url: str | None
    duration_seconds: float | None
    audio_bytes: int | None
    status: EntryStatus
    vocast_episode_id: str | None
    content_hash: str | None
    retry_count: int
    next_retry_at: datetime | None
    claimed_at: datetime | None
    error_message: str | None
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Entry:
        return cls(
            id=row["id"],
            source_id=row["source_id"],
            external_guid=row["external_guid"],
            article_url=row["article_url"],
            title=row["title"],
            author=row["author"],
            published_at=from_iso(row["published_at"]),
            origin_name=row["origin_name"],
            origin_image_url=row["origin_image_url"],
            duration_seconds=row["duration_seconds"],
            audio_bytes=row["audio_bytes"],
            status=EntryStatus(row["status"]),
            vocast_episode_id=row["vocast_episode_id"],
            content_hash=row["content_hash"],
            retry_count=row["retry_count"],
            next_retry_at=from_iso(row["next_retry_at"]),
            claimed_at=from_iso(row["claimed_at"]),
            error_message=row["error_message"],
            created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]),
        )


def _decode_config(raw: str | None) -> dict[str, Any]:
    """Decode a source's config JSON, tolerating null/corrupt values."""
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
