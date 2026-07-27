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

from ..library import LibraryEntry, get_entry, list_entries
from .repository import EntryRepository, PlaylistRepository, PublishedEpisode

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
    #: The upstream publication, used to prefix the episode title.
    origin_name: str | None = None
    #: The narrated text, used as show notes.
    article_text: str | None = None


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
    max_items: int | None = None,
    hide_read_before: datetime | None = None,
) -> list[FeedEpisode]:
    """Assemble feed items, newest published first.

    Driven by the database rather than by scanning the library: with thousands
    of episodes on a network share, reading every meta.json per request costs
    seconds to minutes. Only the entries actually rendered are read.

    max_items caps the feed. Podcast clients do not want, and often cannot
    handle, tens of thousands of items -- especially with article text inlined.
    """
    audio_base = audio_base_url or base_url
    provenance = _provenance_by_episode(
        entries,
        source_id=source_id,
        limit=max_items,
        hide_read_before=hide_read_before,
    )

    episodes: list[FeedEpisode] = []
    seen: set[str] = set()

    for details in provenance:
        seen.add(details.episode_id)
        if details.duration_seconds is not None and details.audio_bytes is not None:
            # Everything the item needs is already recorded, so no filesystem
            # access at all. This is what lets the feed be uncapped: each read
            # of an episode's metadata costs a round trip to a network share.
            episodes.append(_from_details(details, audio_base, token=token))
            continue
        # Recorded before those were stored; fall back to reading the library.
        entry = get_entry(details.episode_id)
        if entry is None:
            continue
        episodes.append(_to_feed_episode(entry, details, audio_base, token=token))

    if source_id is None:
        # Episodes added by hand have no database row, so they still need a
        # library scan -- bounded by the same cap. Excluded ids come from every
        # tracked episode, not just the ones published above: one filtered out
        # for having been downloaded must not reappear as an untracked episode.
        managed = entries.tracked_episode_ids() if entries is not None else set()
        for entry in list_entries(limit=max_items):
            if entry.id in seen or entry.id in managed:
                continue
            episodes.append(_to_feed_episode(entry, None, audio_base, token=token))

    # Newest published first, with an explicit tie-break. Ties are common:
    # feeds that publish a date but no time all land on midnight, so relying on
    # sort stability would make their order depend on query order.
    episodes.sort(key=lambda e: (e.published_at, e.episode_id), reverse=True)
    if max_items is not None:
        return episodes[:max_items]
    return episodes


def collect_playlist_episodes(
    playlists: PlaylistRepository,
    *,
    slug: str,
    base_url: str,
    audio_base_url: str | None = None,
    token: str | None = None,
    max_items: int | None = None,
    hide_read_before: datetime | None = None,
) -> list[FeedEpisode]:
    """Assemble ready playlist items without changing their queue order."""
    audio_base = audio_base_url or base_url
    episodes: list[FeedEpisode] = []
    for item in playlists.published_episodes(
        slug, limit=max_items, hide_read_before=hide_read_before
    ):
        details = item.episode
        if details.duration_seconds is not None and details.audio_bytes is not None:
            episodes.append(_from_details(details, audio_base, token=token))
            continue
        entry = get_entry(details.episode_id)
        if entry is not None:
            episodes.append(_to_feed_episode(entry, details, audio_base, token=token))
    return episodes


def library_entries_to_episodes(
    entries: list[LibraryEntry], *, base_url: str, token: str | None = None
) -> list[FeedEpisode]:
    """Render library entries as feed items with no ingestion provenance."""
    return [_to_feed_episode(entry, None, base_url, token=token) for entry in entries]


def _provenance_by_episode(
    entries: EntryRepository | None,
    *,
    source_id: int | None,
    limit: int | None,
    hide_read_before: datetime | None = None,
) -> list[PublishedEpisode]:
    if entries is None:
        return []
    return entries.published_episodes(
        source_id=source_id,
        limit=limit,
        hide_read_before=hide_read_before,
    )


def _from_details(
    details: PublishedEpisode, base_url: str, *, token: str | None = None
) -> FeedEpisode:
    """Build a feed item purely from database columns."""
    return FeedEpisode(
        episode_id=details.episode_id,
        title=details.title,
        audio_url=with_token(f"{base_url}/audio/{details.episode_id}.mp3", token),
        size_bytes=details.audio_bytes or 0,
        published_at=details.published_at or _episode_id_timestamp(details.episode_id),
        duration_seconds=details.duration_seconds,
        article_url=details.article_url,
        source_name=details.source_name,
        origin_name=details.origin_name,
    )


def _episode_id_timestamp(episode_id: str) -> datetime:
    """Recover synthesis time from the id, which begins with a UTC stamp."""
    try:
        return datetime.strptime(episode_id[:16], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, IndexError):
        return datetime.now(timezone.utc)


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
        published_at=_publication_date(entry, details),
        duration_seconds=entry.duration_seconds,
        # For a manually added episode the library's `source` field is the
        # article URL, so it serves the same purpose as feed provenance.
        article_url=(details.article_url if details else entry.source),
        source_name=details.source_name if details else None,
        origin_name=details.origin_name if details else None,
        article_text=entry.article_text(),
    )


def _publication_date(
    entry: LibraryEntry, details: PublishedEpisode | None
) -> datetime:
    """The article's own publication date, falling back to synthesis time.

    Using the real date means a podcast client orders episodes the way the
    articles were actually published. The trade-off is that narrating an old
    article does not surface it as new; synthesis time is only used when the
    feed gave no date at all.
    """
    if details is not None and details.published_at is not None:
        return details.published_at
    return _synthesized_at(entry)


def _synthesized_at(entry: LibraryEntry) -> datetime:
    """When the audio was produced."""
    try:
        parsed = datetime.fromisoformat(entry.synthesized_at)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def episode_title(episode: FeedEpisode) -> str:
    """`{publication} - {article title}`, when the publication is known.

    A feed aggregating many publications is hard to scan otherwise. The prefix
    is skipped when the title already begins with it, to avoid "Foo - Foo: bar".
    """
    origin = episode.origin_name
    if not origin or episode.title.lower().startswith(origin.lower()):
        return episode.title
    return f"{origin} - {episode.title}"


def episode_description(episode: FeedEpisode) -> str:
    """Plain-text notes, for clients that do not render HTML.

    The narrated text is deliberately not inlined: podcast clients render show
    notes as an undifferentiated wall of text, which reads badly for a full
    article, and it would bloat the feed by megabytes. The text is still stored
    next to the audio (see LibraryEntry.article_path) for anything that wants it.
    """
    if episode.article_url:
        return f"Read the original: {episode.article_url}"
    return episode.title


def episode_notes_html(episode: FeedEpisode) -> str:
    """Show notes as HTML, with the link as a real anchor.

    Clients decide what counts as "show notes" largely by whether the content is
    HTML; a bare plain-text line often ends up shown as a subtitle instead. So
    the notes are emitted as markup, wrapped in CDATA.
    """
    if not episode.article_url:
        return f"<p>{escape(episode.title)}</p>"
    href = escape(episode.article_url, {'"': "&quot;"})
    return f'<p><a href="{href}">Read the original</a></p>'


def _cdata(text: str) -> str:
    """Wrap text in CDATA, splitting any sequence that would close it early."""
    return f"<![CDATA[{text.replace(']]>', ']]]]><![CDATA[>')}]]>"


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
        f"      <title>{escape(episode_title(episode))}</title>",
        f"      <description>{_cdata(episode_notes_html(episode))}</description>",
        (
            f"      <content:encoded>{_cdata(episode_notes_html(episode))}"
            "</content:encoded>"
        ),
        (
            f"      <itunes:summary>{escape(episode_description(episode))}"
            "</itunes:summary>"
        ),
        f'      <guid isPermaLink="false">{escape(episode.episode_id)}</guid>',
        f"      <pubDate>{format_datetime(episode.published_at)}</pubDate>",
        (
            f"      <enclosure url={quoteattr(episode.audio_url)} "
            f'length="{episode.size_bytes}" type="{AUDIO_MIME_TYPE}" />'
        ),
    ]
    if episode.article_url:
        parts.append(f"      <link>{escape(episode.article_url)}</link>")
    author = episode.origin_name or episode.source_name
    if author:
        # itunes:author is where podcast clients surface provenance. RSS's own
        # <source> is deliberately not used: it means "the channel this item
        # came from" and requires that channel's feed URL, which we do not
        # republish.
        parts.append(f"      <itunes:author>{escape(author)}</itunes:author>")
    if episode.duration_seconds is not None:
        parts.append(
            f"      <itunes:duration>{int(episode.duration_seconds)}</itunes:duration>"
        )
    parts.append("    </item>")
    return "\n".join(parts)
