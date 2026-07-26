from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from vocast.ingest.db import open_database
from vocast.ingest.library_query import LibraryQuery, LibraryQueryService
from vocast.ingest.models import EntryStatus, FeedEntry
from vocast.ingest.repository import (
    EntryRepository,
    PlaylistRepository,
    SourceRepository,
)
from vocast.ingest.timeutils import to_iso, utcnow


@pytest.fixture
def library_data(tmp_path: Path):
    db = open_database(tmp_path / "library.db")
    sources = SourceRepository(db)
    entries = EntryRepository(db)
    first_source = sources.add(
        name="FreshRSS", kind="freshrss_api", url="https://reader.example.com"
    )
    second_source = sources.add(
        name="Direct Feed", kind="rss", url="https://direct.example.com/feed"
    )
    now = utcnow()

    def add(
        source_id: int,
        guid: str,
        title: str,
        *,
        origin: str,
        author: str,
        published_offset: int,
        duration: int,
        status: EntryStatus = EntryStatus.READY,
        downloaded: bool = False,
    ):
        entry = entries.insert_if_new(
            FeedEntry(
                source_id=source_id,
                external_guid=guid,
                title=title,
                article_url=f"https://articles.example.com/{guid}",
                published_at=now + timedelta(days=published_offset),
                author=author,
                origin_name=origin,
            )
        )
        if status is EntryStatus.READY:
            entries.mark_ready(
                entry.id,
                episode_id=f"episode-{guid}",
                duration_seconds=duration,
                audio_bytes=duration * 100,
            )
        else:
            entries.set_status(entry.id, status)
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE entries SET duration_seconds = ? WHERE id = ?",
                    (duration, entry.id),
                )
        if downloaded:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE entries SET downloaded_at = ? WHERE id = ?",
                    (to_iso(now), entry.id),
                )
        return entry

    alpha = add(
        first_source.id,
        "alpha",
        "Alpha systems",
        origin="The Daily Planet",
        author="Ada Lovelace",
        published_offset=-2,
        duration=600,
    )
    beta = add(
        first_source.id,
        "beta",
        "Beta release",
        origin="Science Weekly",
        author="Grace Hopper",
        published_offset=-1,
        duration=1200,
        downloaded=True,
    )
    gamma = add(
        second_source.id,
        "gamma",
        "Gamma notes",
        origin="Direct Publication",
        author="Linus Torvalds",
        published_offset=0,
        duration=1800,
        status=EntryStatus.FAILED,
    )
    PlaylistRepository(db).add_entry("listen-later", beta.id)
    return (
        db,
        LibraryQueryService(db),
        first_source,
        second_source,
        alpha,
        beta,
        gamma,
        now,
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (LibraryQuery(search=" alpha "), ["Alpha systems"]),
        (LibraryQuery(search="daily planet"), ["Alpha systems"]),
        (LibraryQuery(search="Grace Hopper"), ["Beta release"]),
        (LibraryQuery(search="Direct Feed"), ["Gamma notes"]),
        (LibraryQuery(search="%"), []),
    ],
)
def test_search_matches_library_metadata(library_data, query, expected):
    _, service, *_ = library_data
    assert [item.title for item in service.search(query).items] == expected


def test_source_and_origin_filters(library_data):
    _, service, first_source, _, *_ = library_data
    by_source = service.search(LibraryQuery(source_id=first_source.id))
    by_origin = service.search(LibraryQuery(origin_id="science weekly"))

    assert {item.title for item in by_source.items} == {"Alpha systems", "Beta release"}
    assert [item.title for item in by_origin.items] == ["Beta release"]


def test_status_queue_and_download_filters(library_data):
    _, service, *_ = library_data
    assert [item.title for item in service.search(LibraryQuery(queued=True)).items] == [
        "Beta release"
    ]
    assert {
        item.title for item in service.search(LibraryQuery(queued=False)).items
    } == {
        "Alpha systems",
        "Gamma notes",
    }
    assert [
        item.title for item in service.search(LibraryQuery(downloaded=True)).items
    ] == ["Beta release"]
    assert [
        item.title
        for item in service.search(LibraryQuery(status=EntryStatus.FAILED)).items
    ] == ["Gamma notes"]


def test_date_and_duration_ranges(library_data):
    _, service, *_, now = library_data
    dates = service.search(
        LibraryQuery(
            published_after=now - timedelta(days=1, hours=1),
            published_before=now - timedelta(hours=1),
        )
    )
    durations = service.search(
        LibraryQuery(min_duration_seconds=1000, max_duration_seconds=1500)
    )

    assert [item.title for item in dates.items] == ["Beta release"]
    assert [item.title for item in durations.items] == ["Beta release"]


def test_sorting_pagination_and_total_count(library_data):
    _, service, *_ = library_data
    first_page = service.search(LibraryQuery(sort="title_asc", page=1, page_size=2))
    second_page = service.search(LibraryQuery(sort="title_asc", page=2, page_size=2))

    assert [item.title for item in first_page.items] == [
        "Alpha systems",
        "Beta release",
    ]
    assert [item.title for item in second_page.items] == ["Gamma notes"]
    assert first_page.total == second_page.total == 3
    assert first_page.pages == 2


def test_invalid_page_values_are_bounded(library_data):
    db, _, *_ = library_data
    service = LibraryQueryService(db, default_page_size=2, max_page_size=2)

    defaulted = service.search(LibraryQuery(page=-4, page_size=0))
    capped = service.search(LibraryQuery(page_size=999))
    fallback_sort = service.search(LibraryQuery(sort="DROP TABLE entries"))

    assert defaulted.query.page == 1
    assert defaulted.query.page_size == capped.query.page_size == 2
    assert fallback_sort.query.sort == "published_desc"
    assert fallback_sort.total == 3


def test_filter_options_are_distinct_and_sorted(library_data):
    _, service, first_source, second_source, *_ = library_data
    assert [source.id for source in service.sources()] == [
        first_source.id,
        second_source.id,
    ]
    assert [origin.name for origin in service.origins()] == [
        "Direct Publication",
        "Science Weekly",
        "The Daily Planet",
    ]


def test_search_and_origin_ids_use_unicode_casefolding(library_data):
    db, service, first_source, *_ = library_data
    entries = EntryRepository(db)
    entry = entries.insert_if_new(
        FeedEntry(
            source_id=first_source.id,
            external_guid="unicode",
            title="Ecole des donnees".replace("E", "É", 1),
            article_url="https://example.com/unicode",
            published_at=utcnow(),
            origin_name="Cafe Revue".replace("e", "é", 1),
        )
    )

    by_title = service.search(LibraryQuery(search="éCOLE"))
    by_origin = service.search(LibraryQuery(origin_id="CAFÉ REVUE"))

    assert [item.entry_id for item in by_title.items] == [entry.id]
    assert [item.entry_id for item in by_origin.items] == [entry.id]
    assert by_origin.items[0].origin_id == "café revue"
