"""Podcast RSS generation for the combined and per-source feeds.

The library is the authority on what audio exists; the ingestion database adds
provenance (which source found an article, and where it came from). Joining
them here means `/feeds/all.xml` lists manually added episodes and ingested
ones side by side, which is what keeps the original `/feed.xml` behavior intact.

Episode GUIDs are library entry ids. Those are assigned once when the audio is
written and never recomputed, so a feed re-render — or a metadata change, or a
restart — cannot make a podcast client re-download an episode.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape, quoteattr

from ..library import LibraryEntry, list_entries
from .repository import EntryRepository, PublishedEpisode

AUDIO_MIME_TYPE = "audio/mpeg"


def with_token(url: str, token: str | None) -> str:
    """Append `?token=` when a feed token is configured."""
    if not token:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}token={urllib.parse.quote(token, safe='')}"


@dataclass(frozen=True)
class FeedEpisode:
    episode_id: str
    title: str
    audio_url: str
    size_bytes: int
    published_at: datetime
    duration_seconds: float | None = None
    article_url: str | None = None
    source_name: str | None = None
    original_published_at: datetime | None = None
    summary: str | None = None


@dataclass(frozen=True)
class FeedChannel:
    title: str
    link: str
    description: str
    image_url: str | None = None


def collect_episodes(
    entries: EntryRepository | None,
    *,
    base_url: str,
    source_id: int | None = None,
    audio_base_url: str | None = None,
    token: str | None = None,
) -> list[FeedEpisode]:
    """Assemble feed items, newest first.

    With source_id set, only that source's episodes are returned. Otherwise
    every library entry is included, whether it came from a feed or from
    `vocast add`.

    audio_base_url points enclosures somewhere other than the feed's own host,
    so a publicly published feed can reference audio that stays private. token
    is appended to enclosure URLs, since a podcast client can only authenticate
    by URL.
    """
    provenance = _provenance_by_episode(entries, source_id=source_id)

    if source_id is not None:
        library_entries = _library_entries_for(provenance)
    else:
        library_entries = list_entries()

    audio_base = audio_base_url or base_url
    episodes: list[FeedEpisode] = []
    for entry in library_entries:
        details = provenance.get(entry.id)
        episodes.append(_to_feed_episode(entry, details, audio_base, token=token))
    return episodes


def library_entries_to_episodes(
    entries: list[LibraryEntry], *, base_url: str, token: str | None = None
) -> list[FeedEpisode]:
    """Render library entries as feed items with no ingestion provenance."""
    return [_to_feed_episode(entry, None, base_url, token=token) for entry in entries]


def _provenance_by_episode(
    entries: EntryRepository | None, *, source_id: int | None
) -> dict[str, PublishedEpisode]:
    if entries is None:
        return {}
    return {e.episode_id: e for e in entries.published_episodes(source_id=source_id)}


def _library_entries_for(
    provenance: dict[str, PublishedEpisode],
) -> list[LibraryEntry]:
    """Look up only the library entries named by a source's episodes.

    Ordering follows list_entries() (newest synthesis first) so a per-source
    feed and the combined feed agree on relative order.
    """
    return [entry for entry in list_entries() if entry.id in provenance]


def _to_feed_episode(
    entry: LibraryEntry,
    details: PublishedEpisode | None,
    base_url: str,
    *,
    token: str | None = None,
) -> FeedEpisode:
    audio_path = entry.audio_path()
    size = audio_path.stat().st_size if audio_path.exists() else 0
    return FeedEpisode(
        episode_id=entry.id,
        title=entry.title,
        audio_url=with_token(f"{base_url}/audio/{entry.id}.mp3", token),
        size_bytes=size,
        published_at=_synthesized_at(entry),
        duration_seconds=entry.duration_seconds,
        # For a manually added episode the library's `source` field is the
        # article URL, so it serves the same purpose as feed provenance.
        article_url=(details.article_url if details else entry.source),
        source_name=details.source_name if details else None,
        original_published_at=details.published_at if details else None,
        summary=details.summary if details else None,
    )


def _synthesized_at(entry: LibraryEntry) -> datetime:
    """When the episode became available.

    This, not the article's own date, is the item's pubDate: it keeps ordering
    identical to the original vocast feed and means a freshly generated episode
    from an old article still shows up as new in a podcast client.
    """
    try:
        parsed = datetime.fromisoformat(entry.synthesized_at)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def episode_description(episode: FeedEpisode) -> str:
    """Human-readable notes: provenance, original date, and a link back."""
    paragraphs: list[str] = []

    provenance: list[str] = []
    if episode.source_name:
        provenance.append(f"From {episode.source_name}.")
    if episode.original_published_at is not None:
        provenance.append(
            f"Originally published {episode.original_published_at.date().isoformat()}."
        )
    if provenance:
        paragraphs.append(" ".join(provenance))

    if episode.summary:
        paragraphs.append(episode.summary)
    if episode.article_url:
        paragraphs.append(f"Read the original: {episode.article_url}")

    # Blank lines only ever separate paragraphs, never lead or trail.
    return "\n\n".join(paragraphs) if paragraphs else episode.title


def build_podcast_rss(channel: FeedChannel, episodes: list[FeedEpisode]) -> str:
    """Render a valid RSS 2.0 podcast feed.

    Every interpolated value is escaped: text through `escape`, attributes
    through `quoteattr`. Titles and URLs come from third-party feeds, so
    treating them as untrusted is not optional.
    """
    items = "\n".join(_render_item(e) for e in episodes)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
        "  <channel>\n"
        f"    <title>{escape(channel.title)}</title>\n"
        f"    <link>{escape(channel.link)}</link>\n"
        f"    <description>{escape(channel.description)}</description>\n"
        "    <language>en-us</language>\n"
        "    <generator>vocast</generator>"
        f"{_render_channel_image(channel)}\n"
        f"{items}\n"
        "  </channel>\n"
        "</rss>\n"
    )


def _render_channel_image(channel: FeedChannel) -> str:
    if not channel.image_url:
        return ""
    href = channel.image_url
    return (
        f"\n    <itunes:image href={quoteattr(href)} />"
        f"\n    <image><url>{escape(href)}</url>"
        f"<title>{escape(channel.title)}</title>"
        f"<link>{escape(channel.link)}</link></image>"
    )


def _render_item(episode: FeedEpisode) -> str:
    parts = [
        "    <item>",
        f"      <title>{escape(episode.title)}</title>",
        f"      <description>{escape(episode_description(episode))}</description>",
        f'      <guid isPermaLink="false">{escape(episode.episode_id)}</guid>',
        f"      <pubDate>{format_datetime(episode.published_at)}</pubDate>",
        (
            f"      <enclosure url={quoteattr(episode.audio_url)} "
            f'length="{episode.size_bytes}" type="{AUDIO_MIME_TYPE}" />'
        ),
    ]
    if episode.article_url:
        parts.append(f"      <link>{escape(episode.article_url)}</link>")
    if episode.source_name:
        # itunes:author is where podcast clients surface provenance. RSS's own
        # <source> is deliberately not used: it means "the channel this item
        # came from" and requires that channel's feed URL, which we do not
        # republish.
        parts.append(
            f"      <itunes:author>{escape(episode.source_name)}</itunes:author>"
        )
    if episode.duration_seconds is not None:
        parts.append(
            f"      <itunes:duration>{int(episode.duration_seconds)}</itunes:duration>"
        )
    parts.append("    </item>")
    return "\n".join(parts)
